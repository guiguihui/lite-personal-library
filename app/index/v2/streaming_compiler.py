"""Bounded-memory compiler for schema-3 compatibility Generations."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifacts import (
    ArtifactRef,
    AtomicHashingSink,
    CandidateReceipt,
    write_canonical_object,
)
from .canonical import canonical_bytes, canonical_hash, iter_canonical_json
from .compiler import (
    _DOC_TYPE_ORDER,
    _global_document,
    _legacy_node_sort_key,
    _node_payload,
    _nonnegative_int,
    _required_mapping,
    _required_sequence,
    _required_string,
    _tree_payload,
    should_prune_body,
)
from .input_proof import INPUT_PROOF_PATH
from .models import COMPILER_SCHEMA_VERSION, CompilerRecipe
from .object_store import StoredSegmentRef, load_segment
from .posting_runs import (
    PostingRecord,
    PostingRunBuilder,
    PostingRunReader,
    merge_posting_runs,
)


@dataclass(frozen=True, slots=True)
class _TokenGroup:
    token: str
    start: object
    rows: int
    body_df: int


def _write_value(sink: AtomicHashingSink, value: object) -> None:
    for fragment in iter_canonical_json(value):
        sink.write_text(fragment)


def _artifact(
    relative_path: str,
    sink: AtomicHashingSink,
    records: int | None,
) -> ArtifactRef:
    return ArtifactRef(
        relative_path=relative_path,
        sha256=sink.sha256,
        byte_size=sink.byte_size,
        records=records,
    )


def _ordered_refs(
    refs: Sequence[StoredSegmentRef],
) -> tuple[StoredSegmentRef, ...]:
    normalized: list[StoredSegmentRef] = []
    seen: set[str] = set()
    for ref in refs:
        if not isinstance(ref, StoredSegmentRef):
            raise TypeError("refs must contain StoredSegmentRef values")
        if ref.doc_type not in _DOC_TYPE_ORDER:
            raise ValueError(f"unsupported document type: {ref.doc_type}")
        if ref.doc_key != f"{ref.doc_type}:{ref.slug}":
            raise ValueError(
                f"segment ref doc_key mismatch: {ref.doc_key!r}"
            )
        if ref.doc_key in seen:
            raise ValueError(f"duplicate document: {ref.doc_key}")
        seen.add(ref.doc_key)
        normalized.append(ref)
    return tuple(
        sorted(
            normalized,
            key=lambda ref: (
                _DOC_TYPE_ORDER.get(ref.doc_type, len(_DOC_TYPE_ORDER)),
                ref.slug,
                ref.doc_key,
            ),
        )
    )


def _input_proof(
    refs: Sequence[StoredSegmentRef],
    compiler_recipe_hash: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "compiler_recipe_hash": compiler_recipe_hash,
        "documents": {
            ref.doc_key: {
                "content_hash": ref.content_hash,
                "segment_recipe_hash": ref.segment_recipe_hash,
            }
            for ref in sorted(refs, key=lambda value: value.doc_key)
        },
    }


def _validate_ref_segment(
    ref: StoredSegmentRef,
    segment: Mapping[str, object],
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    if segment.get("schema_version") != 2:
        raise ValueError("segment.schema_version must be 2")
    document = _required_mapping(segment.get("document"), "segment.document")
    fingerprint = _required_mapping(
        segment.get("fingerprint"), "segment.fingerprint"
    )
    expected = {
        "doc_key": ref.doc_key,
        "type": ref.doc_type,
        "id": ref.slug,
    }
    for field, value in expected.items():
        if document.get(field) != value:
            raise ValueError(
                f"segment ref {field} mismatch for {ref.doc_key}: "
                f"{document.get(field)!r}"
            )
    if fingerprint.get("content_hash") != ref.content_hash:
        raise ValueError(
            f"segment ref content hash mismatch for {ref.doc_key}"
        )
    if fingerprint.get("recipe_hash") != ref.segment_recipe_hash:
        raise ValueError(
            f"segment ref recipe hash mismatch for {ref.doc_key}"
        )
    return document, fingerprint


def _token_groups(reader: PostingRunReader) -> Iterator[_TokenGroup]:
    pending: tuple[PostingRecord, object] | None = None
    while True:
        if pending is None:
            start = reader.mark()
            try:
                first = next(reader)
            except StopIteration:
                return
        else:
            first, start = pending
            pending = None

        token = first.token
        rows = 1
        body_df = int(first.body_tf > 0)
        while True:
            next_start = reader.mark()
            try:
                record = next(reader)
            except StopIteration:
                break
            if record.token != token:
                pending = (record, next_start)
                break
            rows += 1
            body_df += int(record.body_tf > 0)
        yield _TokenGroup(token, start, rows, body_df)


def _unpruned_group_bytes(
    token: str,
    reader: PostingRunReader,
    start: object,
    rows: int,
) -> int:
    reader.rewind(start)
    size = len(canonical_bytes(token)) + 1 + 2
    for index in range(rows):
        record = next(reader)
        total_tf = record.title_tf + record.breadcrumb_tf + record.body_tf
        if index:
            size += 1
        size += 3 + len(str(record.chunk_id)) + len(str(total_tf))
    return size


def _write_inverted_index(
    path: Path,
    merged_run: Path,
    total_chunks: int,
    recipe: CompilerRecipe,
) -> tuple[ArtifactRef, dict[str, object]]:
    sink = AtomicHashingSink(path)
    tokens_before = 0
    tokens_after = 0
    postings_before = 0
    postings_after = 0
    body_tokens_pruned = 0
    body_postings_pruned = 0
    body_tf_pruned = 0
    unpruned_bytes = (
        len(b'{"num_chunks":')
        + len(str(total_chunks))
        + len(b',"postings":{')
        + len(b"}}")
    )

    with PostingRunReader(merged_run) as scanner, PostingRunReader(
        merged_run
    ) as replay, sink:
        sink.write(b'{"num_chunks":')
        sink.write_text(str(total_chunks))
        sink.write(b',"postings":{')
        first_token = True
        for group in _token_groups(scanner):
            tokens_before += 1
            postings_before += group.rows
            if tokens_before > 1:
                unpruned_bytes += 1
            unpruned_bytes += _unpruned_group_bytes(
                group.token,
                replay,
                group.start,
                group.rows,
            )

            prune_body = should_prune_body(
                group.body_df,
                total_chunks,
                min_df=recipe.body_df_min,
                min_coverage=float(recipe.body_df_ratio),
            )
            if prune_body:
                body_tokens_pruned += 1

            replay.rewind(group.start)
            exported = 0
            for _ in range(group.rows):
                record = next(replay)
                if prune_body and record.body_tf:
                    body_postings_pruned += 1
                    body_tf_pruned += record.body_tf
                total_tf = record.title_tf + record.breadcrumb_tf + (
                    0 if prune_body else record.body_tf
                )
                if total_tf > 0:
                    exported += 1

            if exported == 0:
                continue
            if not first_token:
                sink.write(b",")
            first_token = False
            _write_value(sink, group.token)
            sink.write(b":[")
            replay.rewind(group.start)
            emitted = 0
            for _ in range(group.rows):
                record = next(replay)
                total_tf = record.title_tf + record.breadcrumb_tf + (
                    0 if prune_body else record.body_tf
                )
                if total_tf == 0:
                    continue
                if emitted:
                    sink.write(b",")
                sink.write(b"[")
                sink.write_text(str(record.chunk_id))
                sink.write(b",")
                sink.write_text(str(total_tf))
                sink.write(b"]")
                emitted += 1
            sink.write(b"]")
            tokens_after += 1
            postings_after += emitted
        sink.write(b"}}")

    reference = _artifact(
        "inverted-index.json",
        sink,
        tokens_after,
    )
    pruning: dict[str, object] = {
        "body_min_df": recipe.body_df_min,
        "body_min_coverage": float(recipe.body_df_ratio),
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "postings_before": postings_before,
        "postings_after": postings_after,
        "body_tokens_pruned": body_tokens_pruned,
        "body_postings_pruned": body_postings_pruned,
        "body_tf_pruned": body_tf_pruned,
        "estimated_bytes_saved": max(0, unpruned_bytes - reference.byte_size),
    }
    return reference, pruning


def _emit_one_segment(
    *,
    ref: StoredSegmentRef,
    pageindex: Path,
    candidate: Path,
    docs_sink: AtomicHashingSink,
    nodes_sink: AtomicHashingSink,
    chunks_sink: AtomicHashingSink,
    run_builder: PostingRunBuilder,
    docs_count: int,
    nodes_count: int,
    chunks_count: int,
) -> tuple[ArtifactRef, int, int, int]:
    """Load, project, and release exactly one decoded Segment.

    Only scalar counts, posting records, and an artifact receipt escape this
    call frame.  Returning before the caller loads the next ref prevents
    references to nested Segment containers from overlapping across documents.
    """

    segment = load_segment(pageindex, ref)
    document, _fingerprint = _validate_ref_segment(ref, segment)

    if docs_count:
        docs_sink.write(b",")
    _write_value(docs_sink, _global_document(document))
    docs_count += 1

    nodes_value = _required_sequence(
        segment.get("nodes"), "segment.nodes"
    )
    nodes: list[Mapping[str, Any]] = []
    node_by_key: dict[str, Mapping[str, Any]] = {}
    for value in nodes_value:
        node = _required_mapping(value, "segment.nodes[]")
        node_key = _required_string(
            node.get("node_key"), "node.node_key"
        )
        if node_key in node_by_key:
            raise ValueError(
                f"duplicate node_key in {ref.doc_key}: {node_key}"
            )
        node_by_key[node_key] = node
        nodes.append(node)
    nodes.sort(key=_legacy_node_sort_key)
    for node in nodes:
        if nodes_count:
            nodes_sink.write(b",")
        _write_value(
            nodes_sink,
            _node_payload(node, ref.doc_type, ref.slug),
        )
        nodes_count += 1

    tree_path, tree = _tree_payload(
        segment, ref.doc_type, ref.slug
    )
    if not isinstance(tree, Mapping):
        raise ValueError("document tree must be a mapping")
    tree_ref = write_canonical_object(
        candidate / Path(tree_path),
        tree,
        relative_path=tree_path,
    )

    chunks_value = _required_sequence(
        segment.get("chunks"), "segment.chunks"
    )
    chunks = [
        _required_mapping(value, "segment.chunks[]")
        for value in chunks_value
    ]
    chunks.sort(
        key=lambda chunk: (
            _required_string(
                chunk.get("node_key"), "chunk.node_key"
            ),
            _nonnegative_int(
                chunk.get("local_id"), "chunk.local_id"
            ),
        )
    )
    local_to_global: dict[int, int] = {}
    for chunk in chunks:
        local_id = _nonnegative_int(
            chunk.get("local_id"), "chunk.local_id"
        )
        if local_id in local_to_global:
            raise ValueError(
                f"duplicate chunk local_id in {ref.doc_key}: {local_id}"
            )
        node_key = _required_string(
            chunk.get("node_key"), "chunk.node_key"
        )
        node = node_by_key.get(node_key)
        if node is None:
            raise ValueError(
                f"chunk {ref.doc_key}:{local_id} references "
                f"unknown node {node_key}"
            )
        chunks_count += 1
        global_id = chunks_count
        local_to_global[local_id] = global_id
        legacy_node_id = _required_string(
            node.get("legacy_node_id") or node.get("node_id"),
            "node.legacy_node_id",
        )
        breadcrumb = chunk.get("breadcrumb") or []
        if not isinstance(breadcrumb, list) or not all(
            isinstance(part, str) for part in breadcrumb
        ):
            raise ValueError(
                "chunk.breadcrumb must be a list of strings"
            )
        payload = {
            "chunk_id": f"c{global_id:06d}",
            "doc_id": ref.slug,
            "node_id": legacy_node_id,
            "title": str(chunk.get("title") or ""),
            "breadcrumb": list(breadcrumb),
            "body": str(chunk.get("body") or ""),
            "source_md": str(chunk.get("source_md") or ""),
            "line_num": _nonnegative_int(
                chunk.get("line_num", 0), "chunk.line_num"
            ),
        }
        if chunks_count > 1:
            chunks_sink.write(b",")
        _write_value(chunks_sink, payload)

    postings = _required_mapping(
        segment.get("postings"), "segment.postings"
    )
    for raw_token, posting_values in postings.items():
        token = _required_string(raw_token, "posting token")
        posting_sequence = _required_sequence(
            posting_values, f"postings[{token!r}]"
        )
        for item in posting_sequence:
            fields = _required_sequence(
                item, f"postings[{token!r}][]"
            )
            if len(fields) != 4:
                raise ValueError(
                    f"posting for {token!r} must contain "
                    "[local_id, title_tf, breadcrumb_tf, body_tf]"
                )
            local_id = _nonnegative_int(
                fields[0], "posting.local_id"
            )
            global_id = local_to_global.get(local_id)
            if global_id is None:
                raise ValueError(
                    f"posting for {token!r} references unknown "
                    f"chunk {ref.doc_key}:{local_id}"
                )
            run_builder.add(
                PostingRecord(
                    token,
                    global_id,
                    _nonnegative_int(
                        fields[1], "posting.title_tf"
                    ),
                    _nonnegative_int(
                        fields[2], "posting.breadcrumb_tf"
                    ),
                    _nonnegative_int(
                        fields[3], "posting.body_tf"
                    ),
                )
            )

    return tree_ref, docs_count, nodes_count, chunks_count


def _compile_generation_to_candidate(
    refs: Sequence[StoredSegmentRef],
    pageindex_dir: Path,
    candidate_dir: Path,
    recipe: CompilerRecipe,
    *,
    max_run_bytes: int,
    merge_fan_in: int,
) -> CandidateReceipt:
    if not hasattr(recipe, "as_dict"):
        raise TypeError("recipe must provide as_dict()")
    recipe_payload = recipe.as_dict()
    if not isinstance(recipe_payload, Mapping):
        raise TypeError("recipe.as_dict() must return a mapping")
    compiler_recipe_hash = canonical_hash(recipe_payload)
    ordered_refs = _ordered_refs(refs)
    document_hashes = {
        ref.doc_key: ref.segment_hash for ref in ordered_refs
    }
    proof = _input_proof(ordered_refs, compiler_recipe_hash)
    input_proof_sha256 = canonical_hash(proof)
    core_manifest: dict[str, object] = {
        "schema_version": COMPILER_SCHEMA_VERSION,
        "compiler_recipe_hash": compiler_recipe_hash,
        "input_proof_sha256": input_proof_sha256,
        "documents": document_hashes,
    }
    revision_sha256 = canonical_hash(core_manifest)
    generation_id = revision_sha256[:20]

    candidate = Path(candidate_dir)
    if candidate.exists() and any(candidate.iterdir()):
        raise ValueError(f"candidate directory is not empty: {candidate}")
    candidate.mkdir(parents=True, exist_ok=True)
    pageindex = Path(pageindex_dir)
    artifacts: dict[str, ArtifactRef] = {}
    docs_count = 0
    nodes_count = 0
    chunks_count = 0

    with tempfile.TemporaryDirectory(
        dir=candidate.parent,
        prefix=f".{candidate.name}.posting-runs-",
    ) as scratch_name:
        scratch = Path(scratch_name)
        run_builder = PostingRunBuilder(
            scratch / "runs",
            max_run_bytes=max_run_bytes,
        )
        with ExitStack() as stack:
            docs_sink = stack.enter_context(
                AtomicHashingSink(candidate / "global-index.json")
            )
            nodes_sink = stack.enter_context(
                AtomicHashingSink(candidate / "node-index.json")
            )
            chunks_sink = stack.enter_context(
                AtomicHashingSink(candidate / "chunks.json")
            )
            docs_sink.write(b'{"docs":[')
            nodes_sink.write(b'{"nodes":[')
            chunks_sink.write(b'{"chunks":[')

            for ref in ordered_refs:
                (
                    tree_ref,
                    docs_count,
                    nodes_count,
                    chunks_count,
                ) = _emit_one_segment(
                    ref=ref,
                    pageindex=pageindex,
                    candidate=candidate,
                    docs_sink=docs_sink,
                    nodes_sink=nodes_sink,
                    chunks_sink=chunks_sink,
                    run_builder=run_builder,
                    docs_count=docs_count,
                    nodes_count=nodes_count,
                    chunks_count=chunks_count,
                )
                artifacts[tree_ref.relative_path] = tree_ref

            docs_sink.write(b"]}")
            nodes_sink.write(b"]}")
            chunks_sink.write(b"]}")

        artifacts["global-index.json"] = _artifact(
            "global-index.json", docs_sink, docs_count
        )
        artifacts["node-index.json"] = _artifact(
            "node-index.json", nodes_sink, nodes_count
        )
        artifacts["chunks.json"] = _artifact(
            "chunks.json", chunks_sink, chunks_count
        )

        built_runs = run_builder.finish()
        merged = merge_posting_runs(
            built_runs.paths,
            scratch / "postings.pir",
            fan_in=merge_fan_in,
        )
        inverted_ref, pruning = _write_inverted_index(
            candidate / "inverted-index.json",
            merged.path,
            chunks_count,
            recipe,
        )
        artifacts[inverted_ref.relative_path] = inverted_ref

    proof_ref = write_canonical_object(
        candidate / INPUT_PROOF_PATH,
        proof,
        relative_path=INPUT_PROOF_PATH,
        records=len(ordered_refs),
    )
    artifacts[proof_ref.relative_path] = proof_ref
    artifacts = dict(sorted(artifacts.items()))
    stats: dict[str, object] = {
        "documents": docs_count,
        "nodes": nodes_count,
        "chunks": chunks_count,
        "tokens": pruning["tokens_after"],
        "postings": pruning["postings_after"],
    }
    file_metadata = {
        relative_path: {
            "sha256": reference.sha256,
            "bytes": reference.byte_size,
        }
        for relative_path, reference in artifacts.items()
    }
    manifest: dict[str, object] = {
        "schema_version": COMPILER_SCHEMA_VERSION,
        "generation": generation_id,
        "revision_sha256": revision_sha256,
        "compiler_recipe_hash": compiler_recipe_hash,
        "input_proof_sha256": input_proof_sha256,
        "compiler_recipe": dict(recipe_payload),
        "documents": document_hashes,
        "files": file_metadata,
        "stats": stats,
        "pruning": pruning,
        "warnings": [],
    }
    manifest_ref = write_canonical_object(
        candidate / "manifest.json",
        manifest,
        relative_path="manifest.json",
        records=len(artifacts),
    )
    invariants: dict[str, object] = {
        "stats": stats,
        "pruning": pruning,
        "segments_loaded": len(ordered_refs),
        "segments_loaded_peak": int(bool(ordered_refs)),
        "run_buffer_peak_bytes": built_runs.run_buffer_peak_bytes,
        "largest_posting_record_bytes": built_runs.largest_record_bytes,
        "posting_merge_passes": merged.passes,
        "posting_merge_peak_open_inputs": merged.peak_open_inputs,
        "postings_visited": merged.records,
        "generation_bytes_written": manifest_ref.byte_size
        + sum(value.byte_size for value in artifacts.values()),
    }
    return CandidateReceipt(
        candidate_dir=candidate,
        generation_id=generation_id,
        revision_sha256=revision_sha256,
        compiler_recipe_hash=compiler_recipe_hash,
        input_proof_sha256=input_proof_sha256,
        manifest_sha256=manifest_ref.sha256,
        artifacts=artifacts,
        segment_refs={ref.doc_key: ref for ref in ordered_refs},
        invariants=invariants,
    )


def compile_generation_to_candidate(
    refs: Sequence[StoredSegmentRef],
    pageindex_dir: Path,
    candidate_dir: Path,
    recipe: CompilerRecipe,
    *,
    max_run_bytes: int = 32 * 1024 * 1024,
    merge_fan_in: int = 32,
) -> CandidateReceipt:
    """Compile refs directly to a candidate without materializing a Generation."""

    candidate = Path(candidate_dir)
    owns_candidate = False
    try:
        try:
            candidate.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            # A pre-existing path is never owned by this compilation and must
            # therefore survive every validation or compilation failure.
            pass
        else:
            owns_candidate = True
        return _compile_generation_to_candidate(
            refs,
            Path(pageindex_dir),
            candidate,
            recipe,
            max_run_bytes=max_run_bytes,
            merge_fan_in=merge_fan_in,
        )
    except BaseException:
        if owns_candidate and candidate.is_dir():
            shutil.rmtree(candidate, ignore_errors=True)
        raise


__all__ = ["compile_generation_to_candidate"]
