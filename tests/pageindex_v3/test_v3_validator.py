from __future__ import annotations

from collections import Counter
import copy
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any, Callable, Mapping

import pytest

from app.index.v2.artifacts import ArtifactRef
import app.index.v2.compiler as legacy_compiler
import app.index.v2.streaming_compiler as legacy_streaming_compiler
from app.index.v2.canonical import canonical_hash
from app.index.v2.models import SegmentRecipe
from app.index.v2.object_store import StoredSegmentRef, put_segment
from app.index.v2.validator import ValidationReport
from app.index.v3.base_builder import build_base_view
from app.index.v3.delta_builder import DeltaBuildResult, build_delta_view
import app.index.v3.delta_store as delta_store_module
from app.index.v3.delta_store import (
    DeltaObjectReceipt,
    DocumentReplacement,
    StatisticsDelta,
)
import app.index.v3.generation as generation_module
from app.index.v3.generation import LogicalGenerationReceipt, build_logical_generation
import app.index.v3.layer_codec as layer_codec_module
from app.index.v3.layer_codec import (
    LayerDocument,
    PostingLayerReader,
    PostingLayerReceipt,
    TokenContribution,
)
from app.index.v3.layer_runs import build_sorted_layer
from app.index.v3.models import CompactionPolicy, GenerationRecipe, SearchViewRecipe
import app.index.v3.segment_projection as projection_module
from app.index.v3.segment_projection import SegmentProjector
from app.index.v3.source_diff import SegmentChangeSet
from app.index.v3.statistics import CorpusTotals
from app.index.v3.validator import (
    validate_base_normal,
    validate_delta_normal,
    validate_generation_normal,
    validate_view_normal,
)
import app.index.v3.validator as validator_module
import app.index.v3.view_store as view_store_module
from app.index.v3.view_store import (
    BaseObjectReceipt,
    SearchViewReceipt,
    ViewDocumentOwner,
)
from app.retrieval.tokenizer import tokenize


@dataclass(frozen=True, slots=True)
class ValidationCorpus:
    pageindex: Path
    generation_recipe: GenerationRecipe
    search_view_recipe: SearchViewRecipe
    initial_refs: tuple[StoredSegmentRef, ...]
    target_refs: tuple[StoredSegmentRef, ...]
    initial_generation: LogicalGenerationReceipt
    target_generation: LogicalGenerationReceipt
    base: BaseObjectReceipt
    parent: SearchViewReceipt
    delta_result: DeltaBuildResult

    @property
    def delta(self) -> DeltaObjectReceipt:
        return self.delta_result.delta

    @property
    def target(self) -> SearchViewReceipt:
        return self.delta_result.view



def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _document_path(doc_type: str, slug: str) -> str:
    if doc_type == "note":
        return f"notes/{slug}.md"
    return f"{doc_type}s/{slug}/_index.md"


def _segment(
    doc_key: str,
    fields: tuple[tuple[str, tuple[str, ...], str], ...],
    *,
    revision: str,
) -> dict[str, object]:
    doc_type, slug = doc_key.split(":", 1)
    chunks: list[dict[str, object]] = []
    postings: dict[str, list[list[int]]] = {}
    for local_id, (title, breadcrumb, body) in enumerate(fields):
        title_tf = Counter(tokenize(title))
        breadcrumb_tf = Counter(tokenize(" ".join(breadcrumb)))
        body_tf = Counter(tokenize(body))
        chunks.append(
            {
                "local_id": local_id,
                "node_key": "root",
                "title": title,
                "breadcrumb": list(breadcrumb),
                "body": body,
                "lengths": {
                    "title": sum(title_tf.values()),
                    "breadcrumb": sum(breadcrumb_tf.values()),
                    "body": sum(body_tf.values()),
                },
            }
        )
        for token in sorted(
            set(title_tf) | set(breadcrumb_tf) | set(body_tf),
            key=lambda value: value.encode("utf-8"),
        ):
            postings.setdefault(token, []).append(
                [
                    local_id,
                    int(title_tf.get(token, 0)),
                    int(breadcrumb_tf.get(token, 0)),
                    int(body_tf.get(token, 0)),
                ]
            )
    segment_recipe = SegmentRecipe().as_dict()
    source_files = [
        {
            "path": _document_path(doc_type, slug),
            "sha256": hashlib.sha256(
                f"{doc_key}:{revision}".encode("utf-8")
            ).hexdigest(),
        }
    ]
    return {
        "schema_version": 2,
        "segment_recipe": segment_recipe,
        "document": {"doc_key": doc_key, "type": doc_type, "id": slug},
        "fingerprint": {
            "content_hash": canonical_hash(source_files),
            "recipe_hash": canonical_hash(segment_recipe),
            "source_files": source_files,
        },
        "nodes": [{"node_key": "root", "legacy_node_id": "1"}],
        "chunks": chunks,
        "postings": {
            token: postings[token]
            for token in sorted(postings, key=lambda value: value.encode("utf-8"))
        },
    }


def _generation(
    root: Path,
    name: str,
    refs: tuple[StoredSegmentRef, ...],
    recipe: GenerationRecipe,
) -> LogicalGenerationReceipt:
    proof = {
        "schema_version": 1,
        "compiler_recipe_hash": canonical_hash(recipe.as_dict()),
        "documents": {
            ref.doc_key: {
                "content_hash": ref.content_hash,
                "segment_recipe_hash": ref.segment_recipe_hash,
            }
            for ref in reversed(refs)
        },
    }
    return build_logical_generation(refs, proof, recipe, root / name)


def _changes(
    before: tuple[StoredSegmentRef, ...],
    after: tuple[StoredSegmentRef, ...],
) -> SegmentChangeSet:
    old = {ref.doc_key: ref for ref in before}
    new = {ref.doc_key: ref for ref in after}
    changed = {
        key
        for key in old.keys() & new.keys()
        if old[key].segment_hash != new[key].segment_hash
    }
    return SegmentChangeSet(
        base_by_doc=old,
        current_fingerprints={key: new[key].content_hash for key in sorted(new)},
        added=tuple(new.keys() - old.keys()),
        changed=tuple(changed),
        deleted=tuple(old.keys() - new.keys()),
        unchanged=tuple((old.keys() & new.keys()) - changed),
    )


@pytest.fixture
def corpus(tmp_path: Path) -> ValidationCorpus:
    pageindex = tmp_path / "pageindex"
    initial = (
        put_segment(
            pageindex,
            _segment(
                "note:edit",
                (
                    ("alpha stable", ("root",), "old common"),
                    ("second", (), "shared old"),
                ),
                revision="a",
            ),
        ),
        put_segment(
            pageindex,
            _segment("book:keep", (("keep", (), "common"),), revision="a"),
        ),
        put_segment(
            pageindex,
            _segment("note:delete", (("delete", (), "gone"),), revision="a"),
        ),
    )
    by_key = {ref.doc_key: ref for ref in initial}
    target = (
        put_segment(
            pageindex,
            _segment(
                "note:edit",
                (
                    ("alpha fresh", ("root",), "new common"),
                    ("second", (), "shared new"),
                ),
                revision="b",
            ),
        ),
        by_key["book:keep"],
        put_segment(
            pageindex,
            _segment("note:add", (("added", (), "new"),), revision="a"),
        ),
    )
    generation_recipe = GenerationRecipe()
    search_view_recipe = SearchViewRecipe()
    initial_generation = _generation(
        tmp_path, "initial-generation", initial, generation_recipe
    )
    base, parent = build_base_view(
        pageindex,
        initial,
        initial_generation,
        generation_recipe,
        search_view_recipe,
        max_run_bytes=1,
        merge_fan_in=2,
    )
    target_generation = _generation(
        tmp_path, "target-generation", target, generation_recipe
    )
    changes = _changes(initial, target)
    dirty = set(changes.added) | set(changes.changed)
    delta_result = build_delta_view(
        pageindex,
        parent,
        target_generation,
        generation_recipe,
        changes,
        tuple(ref for ref in target if ref.doc_key in dirty),
        search_view_recipe,
        CompactionPolicy(max_delta_layers=99),
        max_run_bytes=1,
        merge_fan_in=2,
    )
    return ValidationCorpus(
        pageindex=pageindex,
        generation_recipe=generation_recipe,
        search_view_recipe=search_view_recipe,
        initial_refs=initial,
        target_refs=target,
        initial_generation=initial_generation,
        target_generation=target_generation,
        base=base,
        parent=parent,
        delta_result=delta_result,
    )

def _assert_ok_report(report: ValidationReport) -> None:
    assert isinstance(report, ValidationReport)
    assert report.ok
    assert report.errors == ()
    assert report.warnings == ()
    assert report.as_dict() == {"ok": True, "errors": [], "warnings": []}


def test_validate_generation_normal_returns_validation_report(
    corpus: ValidationCorpus,
) -> None:
    _assert_ok_report(
        validate_generation_normal(corpus.target_generation, corpus.pageindex)
    )


def test_validate_base_normal_returns_validation_report(
    corpus: ValidationCorpus,
) -> None:
    _assert_ok_report(
        validate_base_normal(corpus.base, corpus.initial_generation, corpus.pageindex)
    )


def test_validate_delta_normal_returns_validation_report(
    corpus: ValidationCorpus,
) -> None:
    _assert_ok_report(
        validate_delta_normal(
            corpus.delta,
            corpus.parent,
            corpus.target,
            corpus.initial_generation,
            corpus.target_generation,
            corpus.pageindex,
        )
    )


def test_validate_view_normal_returns_validation_report(
    corpus: ValidationCorpus,
) -> None:
    _assert_ok_report(
        validate_view_normal(corpus.target, corpus.target_generation, corpus.pageindex)
    )


def test_generation_normal_uses_streaming_validation_only(
    corpus: ValidationCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_stream = validator_module.validate_generation_stream
    stream_collect_refs: list[bool] = []

    def observed_stream(
        receipt: LogicalGenerationReceipt,
        pageindex_dir: Path,
        *,
        check_cancelled: Callable[[], None],
        collect_refs: bool = False,
    ) -> dict[str, StoredSegmentRef]:
        stream_collect_refs.append(collect_refs)
        return original_stream(
            receipt,
            pageindex_dir,
            check_cancelled=check_cancelled,
            collect_refs=collect_refs,
        )

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Generation Normal must not use whole-DOM validation")

    monkeypatch.setattr(
        validator_module, "validate_generation_stream", observed_stream
    )
    monkeypatch.setattr(
        validator_module, "load_bounded_canonical_json", forbidden
    )
    monkeypatch.setattr(
        generation_module, "validate_logical_generation_inputs", forbidden
    )

    _assert_ok_report(
        validate_generation_normal(corpus.target_generation, corpus.pageindex)
    )
    assert stream_collect_refs == [False]


def test_view_normal_reads_only_endpoint_view_data(
    corpus: ValidationCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation, result = _build_second_delta(corpus, tmp_path)
    assert len(result.view.delta_ids) == 2

    original_documents = validator_module.load_view_documents
    original_statistics = validator_module.load_view_statistics
    document_reads: list[str] = []
    statistics_reads: list[str] = []

    def observed_documents(
        receipt: SearchViewReceipt,
    ) -> dict[str, ViewDocumentOwner]:
        document_reads.append(receipt.view_id)
        return original_documents(receipt)

    def observed_statistics(receipt: SearchViewReceipt) -> CorpusTotals:
        statistics_reads.append(receipt.view_id)
        return original_statistics(receipt)

    monkeypatch.setattr(
        validator_module, "load_view_documents", observed_documents
    )
    monkeypatch.setattr(
        validator_module, "load_view_statistics", observed_statistics
    )

    _assert_ok_report(
        validate_view_normal(result.view, generation, corpus.pageindex)
    )
    endpoint_ids = [corpus.parent.view_id, result.view.view_id]
    assert document_reads == endpoint_ids
    assert statistics_reads == endpoint_ids
    assert corpus.target.view_id not in document_reads
    assert corpus.target.view_id not in statistics_reads

def test_dirty_normal_uses_touched_term_windows_without_base_postings_or_v2(
    corpus: ValidationCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_reader_init = PostingLayerReader.__init__
    original_audit = PostingLayerReader.audit
    original_iter_file_lines = layer_codec_module._iter_file_lines
    original_load_segment = projection_module.load_segment
    live_new_segments = {
        replacement.new_segment_hash
        for replacement in corpus.delta.replacements
        if replacement.new_segment_hash is not None
    }
    loaded_segments: Counter[str] = Counter()
    base_reads: Counter[str] = Counter()
    base_root = corpus.base.root.resolve()
    base_terms = (corpus.base.root / "terms.jsonl").resolve()

    def guarded_reader_init(
        reader: PostingLayerReader,
        receipt: PostingLayerReceipt,
        *args: object,
        **kwargs: object,
    ) -> None:
        observer = kwargs.get("read_observer")

        def counted(name: str, offset: int, size: int) -> None:
            if receipt.root.resolve() == base_root:
                base_reads[name] += size
                if name in {
                    "layer-documents.json",
                    "postings.piv",
                    "chunks.pcv",
                }:
                    raise AssertionError(f"dirty Normal must not read Base {name}")
            if observer is not None:
                assert callable(observer)
                observer(name, offset, size)

        kwargs["read_observer"] = counted
        original_reader_init(reader, receipt, *args, **kwargs)

    def guarded_full_term_scan(
        stream: Any,
        byte_size: int,
        observer: Callable[[str, int, int], None] | None = None,
        role: str = "terms.jsonl",
    ):
        stream_name = getattr(stream, "name", None)
        if stream_name is not None and Path(stream_name).resolve() == base_terms:
            raise AssertionError("dirty Normal must not iterate the Base vocabulary")
        yield from original_iter_file_lines(stream, byte_size, observer, role)

    def guarded_audit(reader: PostingLayerReader) -> None:
        if reader.receipt.root.resolve() == base_root:
            raise AssertionError("dirty Normal must not audit the Base layer")
        original_audit(reader)

    def guarded_load_segment(
        pageindex_dir: Path,
        ref: str | StoredSegmentRef,
    ) -> dict[str, object]:
        digest = ref.segment_hash if isinstance(ref, StoredSegmentRef) else ref
        if digest not in live_new_segments:
            raise AssertionError(
                "dirty Normal must load only live new replacement Segments"
            )
        loaded_segments[digest] += 1
        return original_load_segment(pageindex_dir, ref)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Normal validation must not invoke a v2 compiler")

    monkeypatch.setattr(
        projection_module, "load_segment", guarded_load_segment
    )
    monkeypatch.setattr(PostingLayerReader, "__init__", guarded_reader_init)
    monkeypatch.setattr(layer_codec_module, "_iter_file_lines", guarded_full_term_scan)
    monkeypatch.setattr(PostingLayerReader, "audit", guarded_audit)
    monkeypatch.setattr(legacy_compiler, "compile_generation", forbidden)
    monkeypatch.setattr(legacy_compiler, "compile_generation_to_candidate", forbidden)
    monkeypatch.setattr(
        legacy_streaming_compiler,
        "compile_generation_to_candidate",
        forbidden,
    )

    _assert_ok_report(
        validate_delta_normal(
            corpus.delta,
            corpus.parent,
            corpus.target,
            corpus.initial_generation,
            corpus.target_generation,
            corpus.pageindex,
        )
    )
    _assert_ok_report(
        validate_view_normal(corpus.target, corpus.target_generation, corpus.pageindex)
    )
    assert base_reads["layer-documents.json"] == 0
    assert base_reads["postings.piv"] == 0
    assert base_reads["chunks.pcv"] == 0
    assert base_reads["terms.jsonl"] > 0
    assert loaded_segments == Counter(
        {segment_hash: 1 for segment_hash in live_new_segments}
    )

class _Cancelled(RuntimeError):
    pass


@pytest.mark.parametrize("entrypoint", ["generation", "delta"])
def test_normal_validation_propagates_cancellation_unchanged(
    corpus: ValidationCorpus,
    entrypoint: str,
) -> None:
    cancelled = _Cancelled("cancel validator")

    def cancel() -> None:
        raise cancelled

    with pytest.raises(_Cancelled) as observed:
        if entrypoint == "generation":
            validate_generation_normal(
                corpus.target_generation,
                corpus.pageindex,
                check_cancelled=cancel,
            )
        else:
            validate_delta_normal(
                corpus.delta,
                corpus.parent,
                corpus.target,
                corpus.initial_generation,
                corpus.target_generation,
                corpus.pageindex,
                check_cancelled=cancel,
            )
    assert observed.value is cancelled

def _artifact_ref(path: Path, relative_path: str, records: int) -> ArtifactRef:
    raw = path.read_bytes()
    return ArtifactRef(
        relative_path=relative_path,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
        records=records,
    )


def _artifact_dict(reference: ArtifactRef) -> dict[str, object]:
    return {
        "relative_path": reference.relative_path,
        "sha256": reference.sha256,
        "byte_size": reference.byte_size,
        "records": reference.records,
    }


def _assert_invalid_report(report: ValidationReport, code: str) -> None:
    assert isinstance(report, ValidationReport)
    assert not report.ok
    assert report.error_codes == (code,)
    assert report.errors[0].startswith(f"{code}: ")
    assert report.warnings == ()


def _rebind_generation_proof(
    receipt: LogicalGenerationReceipt,
) -> LogicalGenerationReceipt:
    proof_path = receipt.candidate_dir / "input-proof.json"
    proof = json.loads(proof_path.read_bytes())
    proof["compiler_recipe_hash"] = hashlib.sha256(
        b"rebound-proof-recipe"
    ).hexdigest()
    proof_path.write_bytes(_canonical(proof))
    proof_ref = _artifact_ref(
        proof_path,
        "input-proof.json",
        receipt.document_count,
    )

    manifest_path = receipt.candidate_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["input_proof"] = _artifact_dict(proof_ref)
    manifest_path.write_bytes(_canonical(manifest))
    manifest_ref = _artifact_ref(
        manifest_path,
        "manifest.json",
        receipt.document_count,
    )
    return replace(
        receipt,
        input_proof_ref=proof_ref,
        manifest_ref=manifest_ref,
    )


def _rebind_view(
    receipt: SearchViewReceipt,
    recipe: SearchViewRecipe,
    *,
    mutate_documents: Callable[[dict[str, dict[str, object]]], None] | None = None,
    mutate_statistics: Callable[[dict[str, int]], None] | None = None,
    delta_ids: tuple[str, ...] | None = None,
    delta_id_map: Mapping[str, str] | None = None,
    generation: str | None = None,
    generation_manifest_sha256: str | None = None,
) -> SearchViewReceipt:
    root = receipt.root
    documents_path = root / "documents.json"
    documents = json.loads(documents_path.read_bytes())
    if delta_id_map is not None:
        for owner in documents.values():
            owner_id = owner["owner_layer_id"]
            if owner_id in delta_id_map:
                owner["owner_layer_id"] = delta_id_map[owner_id]
    if mutate_documents is not None:
        mutate_documents(documents)
    documents_path.write_bytes(_canonical(documents))
    documents_ref = _artifact_ref(
        documents_path,
        "documents.json",
        len(documents),
    )

    statistics_path = root / "statistics.json"
    statistics = json.loads(statistics_path.read_bytes())
    if mutate_statistics is not None:
        mutate_statistics(statistics)
    statistics_path.write_bytes(_canonical(statistics))
    statistics_ref = _artifact_ref(statistics_path, "statistics.json", 1)

    rebound_deltas = receipt.delta_ids if delta_ids is None else delta_ids
    rebound_generation = receipt.generation if generation is None else generation
    rebound_generation_manifest = (
        receipt.generation_manifest_sha256
        if generation_manifest_sha256 is None
        else generation_manifest_sha256
    )
    core = view_store_module._view_core(
        generation=rebound_generation,
        generation_manifest_sha256=rebound_generation_manifest,
        search_view_recipe_hash=receipt.search_view_recipe_hash,
        base_id=receipt.base_id,
        delta_ids=rebound_deltas,
        statistics_sha256=statistics_ref.sha256,
        documents_sha256=documents_ref.sha256,
    )
    view_id = canonical_hash(core)
    manifest_path = root / "manifest.json"
    manifest_path.write_bytes(
        _canonical(
            {
                **core,
                "view_id": view_id,
                "search_view_recipe": recipe.as_dict(),
                "artifacts": {
                    "statistics": _artifact_dict(statistics_ref),
                    "documents": _artifact_dict(documents_ref),
                },
            }
        )
    )
    manifest_ref = _artifact_ref(manifest_path, "manifest.json", 1)
    destination = root.parent / view_id
    if root != destination:
        root.rename(destination)
    return SearchViewReceipt(
        root=destination,
        view_id=view_id,
        generation=rebound_generation,
        generation_manifest_sha256=rebound_generation_manifest,
        search_view_recipe_hash=receipt.search_view_recipe_hash,
        base_id=receipt.base_id,
        delta_ids=rebound_deltas,
        manifest_ref=manifest_ref,
        statistics_ref=statistics_ref,
        documents_ref=documents_ref,
    )


def _seal_delta(
    corpus: ValidationCorpus,
    *,
    layer: PostingLayerReceipt | None = None,
    statistics_delta: StatisticsDelta | None = None,
    parent_view_id: str | None = None,
    parent_view_manifest_sha256: str | None = None,
    generation: str | None = None,
    generation_manifest_sha256: str | None = None,
    replacements: tuple[DocumentReplacement, ...] | None = None,
) -> DeltaObjectReceipt:
    original = corpus.delta
    rebound_layer = original.layer if layer is None else layer
    rebound_statistics = (
        original.statistics_delta if statistics_delta is None else statistics_delta
    )
    rebound_parent = (
        original.parent_view_id if parent_view_id is None else parent_view_id
    )
    rebound_parent_manifest = (
        original.parent_view_manifest_sha256
        if parent_view_manifest_sha256 is None
        else parent_view_manifest_sha256
    )
    rebound_generation = original.generation if generation is None else generation
    rebound_generation_manifest = (
        original.generation_manifest_sha256
        if generation_manifest_sha256 is None
        else generation_manifest_sha256
    )
    rebound_replacements = (
        original.replacements if replacements is None else replacements
    )
    core = delta_store_module._core(
        parent_view_id=rebound_parent,
        parent_view_manifest_sha256=rebound_parent_manifest,
        generation=rebound_generation,
        generation_manifest_sha256=rebound_generation_manifest,
        search_view_recipe_hash=original.search_view_recipe_hash,
        statistics_delta=rebound_statistics,
        layer=rebound_layer,
        replacements=rebound_replacements,
    )
    delta_id = canonical_hash(core)
    root = rebound_layer.root
    manifest_path = root / "manifest.json"
    manifest_path.write_bytes(
        _canonical(
            {
                **core,
                "delta_id": delta_id,
                "search_view_recipe": corpus.search_view_recipe.as_dict(),
            }
        )
    )
    manifest_ref = _artifact_ref(manifest_path, "manifest.json", 1)
    destination = (
        corpus.pageindex / "objects" / "search" / "deltas" / delta_id
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if root != destination:
        root.rename(destination)
    rebound_layer = replace(rebound_layer, root=destination)
    return DeltaObjectReceipt(
        root=destination,
        delta_id=delta_id,
        parent_view_id=rebound_parent,
        parent_view_manifest_sha256=rebound_parent_manifest,
        generation=rebound_generation,
        generation_manifest_sha256=rebound_generation_manifest,
        search_view_recipe_hash=original.search_view_recipe_hash,
        manifest_ref=manifest_ref,
        layer=rebound_layer,
        statistics_delta=rebound_statistics,
        replacements=rebound_replacements,
    )

def _seal_base(
    corpus: ValidationCorpus,
    *,
    layer: PostingLayerReceipt | None = None,
    statistics: CorpusTotals | None = None,
) -> BaseObjectReceipt:
    original = corpus.base
    rebound_layer = original.layer if layer is None else layer
    rebound_statistics = original.statistics if statistics is None else statistics
    core = view_store_module._base_core(
        generation=original.generation,
        generation_manifest_sha256=original.generation_manifest_sha256,
        search_view_recipe_hash=original.search_view_recipe_hash,
        layer=rebound_layer,
        statistics=rebound_statistics,
    )
    base_id = canonical_hash(core)
    root = rebound_layer.root
    manifest_path = root / "manifest.json"
    manifest_path.write_bytes(
        _canonical(
            {
                **core,
                "base_id": base_id,
                "search_view_recipe": corpus.search_view_recipe.as_dict(),
            }
        )
    )
    manifest_ref = _artifact_ref(manifest_path, "manifest.json", 1)
    destination = corpus.pageindex / "objects" / "search" / "bases" / base_id
    if root != destination:
        root.rename(destination)
    rebound_layer = replace(rebound_layer, root=destination)
    return BaseObjectReceipt(
        root=destination,
        base_id=base_id,
        generation=original.generation,
        generation_manifest_sha256=original.generation_manifest_sha256,
        search_view_recipe_hash=original.search_view_recipe_hash,
        manifest_ref=manifest_ref,
        layer=rebound_layer,
        statistics=rebound_statistics,
    )


def test_generation_rejects_rebound_proof_recipe_binding(
    corpus: ValidationCorpus,
) -> None:
    rebound = _rebind_generation_proof(corpus.target_generation)
    _assert_invalid_report(
        validate_generation_normal(rebound, corpus.pageindex),
        "generation_invalid",
    )


def test_generation_rejects_noncanonical_manifest_with_updated_receipt(
    corpus: ValidationCorpus,
) -> None:
    receipt = corpus.target_generation
    path = receipt.candidate_dir / "manifest.json"
    manifest = json.loads(path.read_bytes())
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8", newline="")
    rebound = replace(
        receipt,
        manifest_ref=_artifact_ref(path, "manifest.json", receipt.document_count),
    )
    _assert_invalid_report(
        validate_generation_normal(rebound, corpus.pageindex),
        "generation_invalid",
    )


def test_base_rejects_rebound_scalar_statistics(
    corpus: ValidationCorpus,
) -> None:
    statistics = replace(
        corpus.base.statistics,
        body_length_sum=corpus.base.statistics.body_length_sum + 1,
    )
    rebound = _seal_base(corpus, statistics=statistics)
    _assert_invalid_report(
        validate_base_normal(rebound, corpus.initial_generation, corpus.pageindex),
        "base_invalid",
    )


def test_base_rejects_rebound_document_to_generation_mapping(
    corpus: ValidationCorpus,
) -> None:
    path = corpus.base.root / "layer-documents.json"
    payload = json.loads(path.read_bytes())
    payload["documents"][0]["segment_hash"] = hashlib.sha256(
        b"wrong-base-segment"
    ).hexdigest()
    path.write_bytes(_canonical(payload))
    layer = replace(
        corpus.base.layer,
        documents=_artifact_ref(
            path,
            "layer-documents.json",
            corpus.base.layer.document_count,
        ),
    )
    rebound = _seal_base(corpus, layer=layer)
    _assert_invalid_report(
        validate_base_normal(rebound, corpus.initial_generation, corpus.pageindex),
        "base_invalid",
    )

@pytest.mark.parametrize("binding", ["parent", "target"])
def test_delta_rejects_rebound_parent_or_target_manifest_hash(
    corpus: ValidationCorpus,
    binding: str,
) -> None:
    wrong = hashlib.sha256(f"wrong-{binding}-manifest".encode()).hexdigest()
    delta = _seal_delta(
        corpus,
        parent_view_manifest_sha256=(wrong if binding == "parent" else None),
        generation_manifest_sha256=(wrong if binding == "target" else None),
    )
    target = _rebind_view(
        corpus.target,
        corpus.search_view_recipe,
        delta_ids=(delta.delta_id,),
        delta_id_map={corpus.delta.delta_id: delta.delta_id},
        generation_manifest_sha256=(wrong if binding == "target" else None),
    )
    _assert_invalid_report(
        validate_delta_normal(
            delta,
            corpus.parent,
            target,
            corpus.initial_generation,
            corpus.target_generation,
            corpus.pageindex,
        ),
        "delta_invalid",
    )


def test_delta_rejects_scalar_transition_rebound_into_target_view(
    corpus: ValidationCorpus,
) -> None:
    statistics_delta = replace(
        corpus.delta.statistics_delta,
        title_length_sum=corpus.delta.statistics_delta.title_length_sum + 1,
    )
    delta = _seal_delta(corpus, statistics_delta=statistics_delta)

    def mutate_statistics(value: dict[str, int]) -> None:
        value["title_length_sum"] += 1

    target = _rebind_view(
        corpus.target,
        corpus.search_view_recipe,
        delta_ids=(delta.delta_id,),
        delta_id_map={corpus.delta.delta_id: delta.delta_id},
        mutate_statistics=mutate_statistics,
    )
    _assert_invalid_report(
        validate_delta_normal(
            delta,
            corpus.parent,
            target,
            corpus.initial_generation,
            corpus.target_generation,
            corpus.pageindex,
        ),
        "delta_invalid",
    )


@pytest.mark.parametrize("drift", ["route", "ordinal", "summary"])
def test_delta_rejects_rebound_target_owner_drift(
    corpus: ValidationCorpus,
    drift: str,
) -> None:
    def mutate(documents: dict[str, dict[str, object]]) -> None:
        delta_owners = [
            owner
            for owner in documents.values()
            if owner["owner_layer_kind"] == "delta"
        ]
        assert len(delta_owners) == 2
        owner = delta_owners[0]
        if drift == "route":
            owner["owner_layer_kind"] = "base"
            owner["owner_layer_id"] = corpus.base.base_id
        elif drift == "ordinal":
            owner["doc_ordinal"] = delta_owners[1]["doc_ordinal"]
        else:
            owner["summary_sha256"] = hashlib.sha256(
                b"wrong-owner-summary"
            ).hexdigest()

    target = _rebind_view(
        corpus.target,
        corpus.search_view_recipe,
        mutate_documents=mutate,
    )
    _assert_invalid_report(
        validate_delta_normal(
            corpus.delta,
            corpus.parent,
            target,
            corpus.initial_generation,
            corpus.target_generation,
            corpus.pageindex,
        ),
        "delta_invalid",
    )


def test_view_rejects_rebound_final_owner_summary(
    corpus: ValidationCorpus,
) -> None:
    def mutate(documents: dict[str, dict[str, object]]) -> None:
        owner = next(
            value
            for value in documents.values()
            if value["owner_layer_kind"] == "delta"
        )
        owner["summary_sha256"] = hashlib.sha256(
            b"wrong-final-owner-summary"
        ).hexdigest()

    target = _rebind_view(
        corpus.target,
        corpus.search_view_recipe,
        mutate_documents=mutate,
    )
    _assert_invalid_report(
        validate_view_normal(target, corpus.target_generation, corpus.pageindex),
        "view_invalid",
    )

def _delta_layer_inputs(
    corpus: ValidationCorpus,
) -> tuple[list[LayerDocument], list[object], list[TokenContribution]]:
    wanted = {
        replacement.new_segment_hash
        for replacement in corpus.delta.replacements
        if replacement.new_segment_hash is not None
    }
    projector = SegmentProjector(corpus.pageindex)
    documents: list[LayerDocument] = []
    postings: list[object] = []
    for ref in corpus.target_refs:
        if ref.segment_hash not in wanted:
            continue
        projection = projector.project(ref)
        documents.append(
            LayerDocument(
                projection.summary.doc_key,
                projection.summary.doc_uid,
                projection.summary.segment_hash,
                projection.chunk_metrics,
            )
        )
        postings.extend(projection.postings)
    documents.sort(key=lambda item: item.doc_uid.encode("utf-8"))
    contributions = [
        TokenContribution(
            value["token"],
            value["df_any_delta"],
            value["df_nonbody_delta"],
            value["df_body_delta"],
        )
        for line in (corpus.delta.root / "terms.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        for value in (json.loads(line),)
    ]
    return documents, postings, contributions


def _target_for_rebound_delta(
    corpus: ValidationCorpus,
    delta: DeltaObjectReceipt,
) -> SearchViewReceipt:
    return _rebind_view(
        corpus.target,
        corpus.search_view_recipe,
        delta_ids=(delta.delta_id,),
        delta_id_map={corpus.delta.delta_id: delta.delta_id},
    )


def test_delta_rejects_rebound_touched_term_arithmetic(
    corpus: ValidationCorpus,
    tmp_path: Path,
) -> None:
    documents, postings, contributions = _delta_layer_inputs(corpus)
    assert contributions
    contributions[0] = replace(
        contributions[0],
        df_any_delta=contributions[0].df_any_delta + 1,
    )
    layer = build_sorted_layer(
        tmp_path / "term-arithmetic-layer",
        documents=documents,
        postings=postings,
        token_contributions=contributions,
        layer_kind="delta",
        recipe=corpus.search_view_recipe,
        max_run_bytes=1,
        merge_fan_in=2,
    )
    delta = _seal_delta(corpus, layer=layer)
    target = _target_for_rebound_delta(corpus, delta)
    _assert_invalid_report(
        validate_delta_normal(
            delta,
            corpus.parent,
            target,
            corpus.initial_generation,
            corpus.target_generation,
            corpus.pageindex,
        ),
        "delta_invalid",
    )


def test_delta_rejects_rebound_posting_tf_with_unchanged_df(
    corpus: ValidationCorpus,
    tmp_path: Path,
) -> None:
    documents, postings, contributions = _delta_layer_inputs(corpus)
    position = next(
        index
        for index, posting in enumerate(postings)
        if getattr(posting, "title_tf") > 0
    )
    postings[position] = replace(
        postings[position],
        title_tf=getattr(postings[position], "title_tf") + 7,
    )
    layer = build_sorted_layer(
        tmp_path / "tf-rebound-layer",
        documents=documents,
        postings=postings,
        token_contributions=contributions,
        layer_kind="delta",
        recipe=corpus.search_view_recipe,
        max_run_bytes=1,
        merge_fan_in=2,
    )
    delta = _seal_delta(corpus, layer=layer)
    target = _target_for_rebound_delta(corpus, delta)
    _assert_invalid_report(
        validate_delta_normal(
            delta,
            corpus.parent,
            target,
            corpus.initial_generation,
            corpus.target_generation,
            corpus.pageindex,
        ),
        "delta_invalid",
    )


def test_delta_rejects_consistent_summary_pcv_statistics_owner_drift(
    corpus: ValidationCorpus,
    tmp_path: Path,
) -> None:
    documents, postings, contributions = _delta_layer_inputs(corpus)
    document = documents[0]
    metrics = list(document.chunk_metrics)
    metrics[0] = replace(
        metrics[0], title_length=metrics[0].title_length + 1
    )
    documents[0] = replace(document, chunk_metrics=tuple(metrics))
    layer = build_sorted_layer(
        tmp_path / "summary-pcv-rebound-layer",
        documents=documents,
        postings=postings,
        token_contributions=contributions,
        layer_kind="delta",
        recipe=corpus.search_view_recipe,
        max_run_bytes=1,
        merge_fan_in=2,
    )

    replacement = next(
        value
        for value in corpus.delta.replacements
        if value.doc_uid == document.doc_uid
    )
    assert replacement.new_segment_hash is not None
    digest = replacement.new_segment_hash
    summary_path = (
        corpus.pageindex
        / "objects"
        / "search"
        / "summaries"
        / digest[:2]
        / f"{digest}.json"
    )
    summary = json.loads(summary_path.read_bytes())
    summary["field_length_sums"]["title"] += 1
    summary_path.write_bytes(_canonical(summary))
    summary_ref = _artifact_ref(summary_path, summary_path.name, 1)
    replacements = tuple(
        replace(
            value,
            new_summary_sha256=summary_ref.sha256,
            new_summary_bytes=summary_ref.byte_size,
        )
        if value.doc_uid == document.doc_uid
        else value
        for value in corpus.delta.replacements
    )
    statistics_delta = replace(
        corpus.delta.statistics_delta,
        title_length_sum=corpus.delta.statistics_delta.title_length_sum + 1,
    )
    delta = _seal_delta(
        corpus,
        layer=layer,
        statistics_delta=statistics_delta,
        replacements=replacements,
    )

    def mutate_statistics(value: dict[str, int]) -> None:
        value["title_length_sum"] += 1

    def mutate_documents(
        value: dict[str, dict[str, object]],
    ) -> None:
        owner = value[document.doc_uid]
        owner["summary_sha256"] = summary_ref.sha256
        owner["summary_bytes"] = summary_ref.byte_size

    target = _rebind_view(
        corpus.target,
        corpus.search_view_recipe,
        delta_ids=(delta.delta_id,),
        delta_id_map={corpus.delta.delta_id: delta.delta_id},
        mutate_statistics=mutate_statistics,
        mutate_documents=mutate_documents,
    )
    _assert_invalid_report(
        validate_delta_normal(
            delta,
            corpus.parent,
            target,
            corpus.initial_generation,
            corpus.target_generation,
            corpus.pageindex,
        ),
        "delta_invalid",
    )


@pytest.mark.parametrize("include_wrong_postings", [False, True], ids=["pcv", "piv-pcv"])
def test_delta_rejects_rows_outside_live_new_replacements(
    corpus: ValidationCorpus,
    tmp_path: Path,
    include_wrong_postings: bool,
) -> None:
    documents, postings, contributions = _delta_layer_inputs(corpus)
    omitted = documents[0]
    documents = documents[1:]
    postings = [
        row
        for row in postings
        if getattr(row, "chunk_ref").doc_uid != omitted.doc_uid
    ]
    target_keys = {ref.doc_key for ref in corpus.target_refs}
    wrong_ref = next(ref for ref in corpus.initial_refs if ref.doc_key not in target_keys)
    wrong_projection = SegmentProjector(corpus.pageindex).project(wrong_ref)
    documents.append(
        LayerDocument(
            wrong_projection.summary.doc_key,
            wrong_projection.summary.doc_uid,
            wrong_projection.summary.segment_hash,
            wrong_projection.chunk_metrics,
        )
    )
    documents.sort(key=lambda item: item.doc_uid.encode("utf-8"))
    if include_wrong_postings:
        postings.extend(wrong_projection.postings)
    posting_tokens = {getattr(row, "token") for row in postings}
    contributions = [
        contribution
        for contribution in contributions
        if contribution.triple != (0, 0, 0)
        or contribution.token in posting_tokens
    ]
    layer = build_sorted_layer(
        tmp_path / f"replacement-scope-{include_wrong_postings}",
        documents=documents,
        postings=postings,
        token_contributions=contributions,
        layer_kind="delta",
        recipe=corpus.search_view_recipe,
        max_run_bytes=1,
        merge_fan_in=2,
    )
    delta = _seal_delta(corpus, layer=layer)
    target = _target_for_rebound_delta(corpus, delta)
    _assert_invalid_report(
        validate_delta_normal(
            delta,
            corpus.parent,
            target,
            corpus.initial_generation,
            corpus.target_generation,
            corpus.pageindex,
        ),
        "delta_invalid",
    )


def test_delta_rejects_changed_summary_file_drift(
    corpus: ValidationCorpus,
) -> None:
    replacement = next(
        value
        for value in corpus.delta.replacements
        if value.new_segment_hash is not None
    )
    assert replacement.new_segment_hash is not None
    digest = replacement.new_segment_hash
    path = (
        corpus.pageindex
        / "objects"
        / "search"
        / "summaries"
        / digest[:2]
        / f"{digest}.json"
    )
    path.write_bytes(path.read_bytes() + b" ")
    _assert_invalid_report(
        validate_delta_normal(
            corpus.delta,
            corpus.parent,
            corpus.target,
            corpus.initial_generation,
            corpus.target_generation,
            corpus.pageindex,
        ),
        "delta_invalid",
    )

def _build_second_delta(
    corpus: ValidationCorpus,
    tmp_path: Path,
) -> tuple[LogicalGenerationReceipt, DeltaBuildResult]:
    prior = {ref.doc_key: ref for ref in corpus.target_refs}
    second_refs = tuple(
        put_segment(
            corpus.pageindex,
            _segment(
                "book:keep",
                (("keep revised", (), "common later"),),
                revision="b",
            ),
        )
        if ref.doc_key == "book:keep"
        else ref
        for ref in corpus.target_refs
    )
    generation = _generation(
        tmp_path,
        "second-target-generation",
        second_refs,
        corpus.generation_recipe,
    )
    changes = _changes(corpus.target_refs, second_refs)
    new_refs = tuple(
        ref
        for ref in second_refs
        if ref.doc_key in set(changes.added) | set(changes.changed)
    )
    result = build_delta_view(
        corpus.pageindex,
        corpus.target,
        generation,
        corpus.generation_recipe,
        changes,
        new_refs,
        corpus.search_view_recipe,
        CompactionPolicy(max_delta_layers=99),
        max_run_bytes=1,
        merge_fan_in=2,
    )
    assert prior["book:keep"].segment_hash != next(
        ref.segment_hash for ref in second_refs if ref.doc_key == "book:keep"
    )
    return generation, result


def _forge_rebound_view_chain(
    receipt: SearchViewReceipt,
    recipe: SearchViewRecipe,
    delta_ids: tuple[str, ...],
) -> SearchViewReceipt:
    core = view_store_module._view_core(
        generation=receipt.generation,
        generation_manifest_sha256=receipt.generation_manifest_sha256,
        search_view_recipe_hash=receipt.search_view_recipe_hash,
        base_id=receipt.base_id,
        delta_ids=delta_ids,
        statistics_sha256=receipt.statistics_ref.sha256,
        documents_sha256=receipt.documents_ref.sha256,
    )
    view_id = canonical_hash(core)
    manifest_path = receipt.root / "manifest.json"
    manifest_path.write_bytes(
        _canonical(
            {
                **core,
                "view_id": view_id,
                "search_view_recipe": recipe.as_dict(),
                "artifacts": {
                    "statistics": _artifact_dict(receipt.statistics_ref),
                    "documents": _artifact_dict(receipt.documents_ref),
                },
            }
        )
    )
    manifest_ref = _artifact_ref(manifest_path, "manifest.json", 1)
    destination = receipt.root.parent / view_id
    receipt.root.rename(destination)
    forged = copy.copy(receipt)
    object.__setattr__(forged, "root", destination)
    object.__setattr__(forged, "view_id", view_id)
    object.__setattr__(forged, "delta_ids", delta_ids)
    object.__setattr__(forged, "manifest_ref", manifest_ref)
    return forged


def test_view_rejects_reordered_delta_chain(
    corpus: ValidationCorpus,
    tmp_path: Path,
) -> None:
    generation, result = _build_second_delta(corpus, tmp_path)
    assert result.view.delta_ids == (
        corpus.delta.delta_id,
        result.delta.delta_id,
    )
    reordered = _rebind_view(
        result.view,
        corpus.search_view_recipe,
        delta_ids=tuple(reversed(result.view.delta_ids)),
    )
    _assert_invalid_report(
        validate_view_normal(reordered, generation, corpus.pageindex),
        "view_invalid",
    )


def test_view_rejects_spliced_unknown_delta(
    corpus: ValidationCorpus,
    tmp_path: Path,
) -> None:
    generation, result = _build_second_delta(corpus, tmp_path)
    missing = hashlib.sha256(b"spliced-delta").hexdigest()
    spliced = _rebind_view(
        result.view,
        corpus.search_view_recipe,
        delta_ids=(corpus.delta.delta_id, missing),
        delta_id_map={result.delta.delta_id: missing},
    )
    _assert_invalid_report(
        validate_view_normal(spliced, generation, corpus.pageindex),
        "view_invalid",
    )


def test_view_rejects_repeated_delta_cycle(
    corpus: ValidationCorpus,
    tmp_path: Path,
) -> None:
    generation, result = _build_second_delta(corpus, tmp_path)
    cyclic = _forge_rebound_view_chain(
        result.view,
        corpus.search_view_recipe,
        result.view.delta_ids + (result.view.delta_ids[0],),
    )
    _assert_invalid_report(
        validate_view_normal(cyclic, generation, corpus.pageindex),
        "view_invalid",
    )