"""Incremental PageIndex v3 document-replacement Delta construction.

The builder authenticates only control-plane metadata and touched sparse term
windows from the parent.  Old Segments are never decoded; only add/edit
Segments are projected, one at a time, into a new immutable Delta layer.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from pathlib import Path
import shutil
import tempfile
import threading

from app.index.v2.canonical import canonical_hash
from app.index.v2.object_store import StoredSegmentRef

from .delta_store import (
    DeltaObjectReceipt,
    DeltaStoreConflictError,
    DocumentReplacement,
    StatisticsDelta,
    finalize_delta_object,
    load_delta_object_metadata,
    write_delta_candidate,
)
from .generation import (
    LogicalGenerationReceipt,
    validate_logical_generation_inputs,
)
from .incremental_witness import ParentDfWitness
from .layer_codec import PostingLayerReader, PostingLayerReceipt, TokenContribution
from .layer_runs import StagedLayerBuilder
from .models import (
    MAX_U64,
    CompactionPolicy,
    GenerationRecipe,
    SearchViewRecipe,
    SegmentSummary,
    make_doc_uid,
)
from .segment_projection import SegmentProjector
from .source_diff import SegmentChangeSet
from .summary_store import StoredSummaryRef, load_summary, put_summary
from .view_store import (
    BaseObjectReceipt,
    SearchViewReceipt,
    ViewDocumentOwner,
    ViewStoreConflictError,
    finalize_search_view,
    load_base_object_metadata,
    load_search_view_metadata,
    load_view_documents,
    load_view_statistics,
    write_search_view_candidate,
)


_SCALAR_FIELDS = (
    "documents",
    "total_chunks",
    "title_length_sum",
    "breadcrumb_length_sum",
    "body_length_sum",
    "posting_count",
)
_MAX_PARENT_LOOKUP_WORKERS = 8
_PARENT_LOOKUP_CANCEL_POLL_SECONDS = 0.05


class _ParentLookupAborted(RuntimeError):
    """Stop worker lookups after main-thread cancellation or failure."""


def _u64(value: object, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_U64
    ):
        raise ValueError(f"{field} must be a u64")
    return value


def _signed_u64(value: object, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < -MAX_U64
        or value > MAX_U64
    ):
        raise ValueError(f"{field} must be in [-{MAX_U64}, {MAX_U64}]")
    return value


def _checked_u64_add(field: str, left: int, right: int) -> int:
    return _u64(left + right, field)


def _checked_signed_add(field: str, left: int, right: int) -> int:
    return _signed_u64(left + right, field)


@dataclass(frozen=True, slots=True)
class DeltaBuildWork:
    old_summaries_loaded: int
    old_segments_loaded: int
    new_segments_loaded: int
    new_summaries_built: int
    projected_postings: int
    touched_tokens: int
    parent_term_windows_read: int
    base_posting_bytes_read: int
    bytes_written: int
    layer_count: int
    segments_loaded_peak: int

    def __post_init__(self) -> None:
        for field in self.__dataclass_fields__:
            _u64(getattr(self, field), field)


@dataclass(frozen=True, slots=True)
class CompactionRecommendation:
    recommended: bool
    layer_limit_reached: bool
    byte_ratio_reached: bool
    delta_layers: int
    base_bytes: int
    delta_bytes: int

    def __post_init__(self) -> None:
        for field in (
            "recommended",
            "layer_limit_reached",
            "byte_ratio_reached",
        ):
            if type(getattr(self, field)) is not bool:
                raise TypeError(f"{field} must be a bool")
        if self.recommended != (
            self.layer_limit_reached or self.byte_ratio_reached
        ):
            raise ValueError("recommended must equal the threshold union")
        for field in ("delta_layers", "base_bytes", "delta_bytes"):
            _u64(getattr(self, field), field)


@dataclass(frozen=True, slots=True)
class DeltaBuildResult:
    delta: DeltaObjectReceipt
    view: SearchViewReceipt
    compaction: CompactionRecommendation
    work: DeltaBuildWork
    parent_df_witness: ParentDfWitness = field(
        kw_only=True,
        repr=False,
        compare=False,
        hash=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.delta, DeltaObjectReceipt):
            raise TypeError("delta must be a DeltaObjectReceipt")
        if not isinstance(self.view, SearchViewReceipt):
            raise TypeError("view must be a SearchViewReceipt")
        if not isinstance(self.compaction, CompactionRecommendation):
            raise TypeError("compaction must be a CompactionRecommendation")
        if not isinstance(self.work, DeltaBuildWork):
            raise TypeError("work must be a DeltaBuildWork")
        if not isinstance(self.parent_df_witness, ParentDfWitness):
            raise TypeError("parent_df_witness must be a ParentDfWitness")
        if len(self.parent_df_witness.contributions) != self.work.touched_tokens:
            raise ValueError("parent DF witness must exactly cover touched tokens")


def _validate_new_refs(
    refs: Iterable[StoredSegmentRef],
    changes: SegmentChangeSet,
) -> dict[str, StoredSegmentRef]:
    if isinstance(refs, (str, bytes, bytearray)):
        raise TypeError("new_refs must be an iterable of StoredSegmentRef values")
    try:
        values = tuple(refs)
    except TypeError as exc:
        raise TypeError(
            "new_refs must be an iterable of StoredSegmentRef values"
        ) from exc
    expected = set(changes.added) | set(changes.changed)
    result: dict[str, StoredSegmentRef] = {}
    for ref in values:
        if not isinstance(ref, StoredSegmentRef):
            raise TypeError("new_refs must contain only StoredSegmentRef values")
        if ref.doc_key in result:
            raise ValueError(f"duplicate new Segment ref: {ref.doc_key}")
        if ref.doc_key not in expected:
            raise ValueError(f"unexpected new Segment ref: {ref.doc_key}")
        if ref.content_hash != changes.current_fingerprints[ref.doc_key]:
            raise ValueError(
                f"new Segment content hash does not match changes: {ref.doc_key}"
            )
        old = changes.base_by_doc.get(ref.doc_key)
        if old is not None and old.segment_hash == ref.segment_hash:
            raise ValueError(
                f"changed document retained its old Segment: {ref.doc_key}"
            )
        result[ref.doc_key] = ref
    if set(result) != expected:
        missing = sorted(expected - set(result))
        raise ValueError(f"new_refs do not exactly cover added/changed docs: {missing}")
    return result


def _authenticate_parent(
    root: Path,
    parent: SearchViewReceipt,
    changes: SegmentChangeSet,
    check_cancelled: Callable[[], None],
) -> tuple[
    SearchViewReceipt,
    BaseObjectReceipt,
    dict[str, ViewDocumentOwner],
    tuple[DeltaObjectReceipt, ...],
]:
    check_cancelled()
    authenticated = load_search_view_metadata(root, parent.view_id)
    check_cancelled()
    if authenticated.attestation_dict() != parent.attestation_dict():
        raise ValueError("parent receipt does not match the finalized Search View")
    if authenticated.root.resolve() != Path(parent.root).resolve():
        raise ValueError("parent receipt root does not match the finalized Search View")
    owners = load_view_documents(authenticated)
    check_cancelled()
    if len(owners) != len(changes.base_by_doc):
        raise ValueError("changes.base_by_doc does not exactly match parent owners")
    for position, (doc_key, ref) in enumerate(changes.base_by_doc.items()):
        if position % 1024 == 0:
            check_cancelled()
        doc_uid = make_doc_uid(doc_key)
        owner = owners.get(doc_uid)
        if owner is None or owner.doc_key != doc_key:
            raise ValueError(f"parent owner identity mismatch for {doc_key}")
        if owner.segment_hash != ref.segment_hash:
            raise ValueError(f"parent owner Segment mismatch for {doc_key}")

    check_cancelled()
    base = load_base_object_metadata(root, authenticated.base_id)
    check_cancelled()
    deltas: list[DeltaObjectReceipt] = []
    prefix: tuple[str, ...] = ()
    for delta_id in authenticated.delta_ids:
        check_cancelled()
        delta = load_delta_object_metadata(root, delta_id)
        if delta.search_view_recipe_hash != authenticated.search_view_recipe_hash:
            raise ValueError("parent Delta SearchViewRecipe does not match its View")
        declared_parent = load_search_view_metadata(root, delta.parent_view_id)
        if declared_parent.manifest_ref.sha256 != delta.parent_view_manifest_sha256:
            raise ValueError("parent Delta is rebound from its declared parent View")
        if declared_parent.base_id != authenticated.base_id:
            raise ValueError("parent Delta chain changes Base identity")
        if declared_parent.search_view_recipe_hash != authenticated.search_view_recipe_hash:
            raise ValueError("parent Delta chain changes SearchViewRecipe")
        if declared_parent.delta_ids != prefix:
            raise ValueError("parent Delta chain is not chronologically contiguous")
        expected_generation = deltas[-1] if deltas else base
        if (
            declared_parent.generation != expected_generation.generation
            or declared_parent.generation_manifest_sha256
            != expected_generation.generation_manifest_sha256
        ):
            raise ValueError("parent Delta chain Generation boundary is invalid")
        if delta.generation == declared_parent.generation:
            raise ValueError("parent Delta target Generation does not advance")
        deltas.append(delta)
        prefix += (delta.delta_id,)
    if deltas:
        last = deltas[-1]
        if (
            last.generation != authenticated.generation
            or last.generation_manifest_sha256
            != authenticated.generation_manifest_sha256
        ):
            raise ValueError("last parent Delta does not bind the parent Generation")
    elif (
        base.generation != authenticated.generation
        or base.generation_manifest_sha256
        != authenticated.generation_manifest_sha256
    ):
        raise ValueError("zero-Delta parent does not bind its Base Generation")
    check_cancelled()
    return authenticated, base, owners, tuple(deltas)


def _old_summary_ref(
    ref: StoredSegmentRef,
    doc_uid: str,
    owner: ViewDocumentOwner,
) -> StoredSummaryRef:
    return StoredSummaryRef(
        segment_hash=ref.segment_hash,
        summary_sha256=owner.summary_sha256,
        byte_size=owner.summary_bytes,
        doc_key=ref.doc_key,
        doc_uid=doc_uid,
        content_hash=ref.content_hash,
        segment_recipe_hash=ref.segment_recipe_hash,
    )


def _accumulate_summary(
    summary: SegmentSummary,
    sign: int,
    scalar_delta: dict[str, int],
    token_delta: dict[str, list[int]],
    touched_tokens: set[str],
    new_posting_tokens: set[str],
) -> None:
    values = {
        "documents": 1,
        "total_chunks": summary.chunk_count,
        "title_length_sum": summary.title_length_sum,
        "breadcrumb_length_sum": summary.breadcrumb_length_sum,
        "body_length_sum": summary.body_length_sum,
        "posting_count": summary.posting_count,
    }
    for field, value in values.items():
        scalar_delta[field] = _checked_signed_add(
            field, scalar_delta[field], sign * value
        )
    for token in summary.tokens:
        touched_tokens.add(token.token)
        if sign > 0:
            new_posting_tokens.add(token.token)
        delta = token_delta.setdefault(token.token, [0, 0, 0])
        for position, value in enumerate(
            (token.df_any, token.df_nonbody, token.df_body)
        ):
            delta[position] = _checked_signed_add(
                f"{token.token!r} token delta",
                delta[position],
                sign * value,
            )


def _validate_df_triple(
    token: str,
    value: tuple[int, int, int],
    total_chunks: int,
    state: str,
) -> None:
    any_df, nonbody_df, body_df = value
    if min(value) < 0 or max(value) > total_chunks:
        raise ValueError(f"{token!r} {state} DF is outside corpus chunk bounds")
    if max(nonbody_df, body_df) > any_df or any_df > nonbody_df + body_df:
        raise ValueError(f"{token!r} {state} DF union is invalid")


def _resolve_parent_terms(
    touched_tokens: set[str],
    base: BaseObjectReceipt,
    deltas: tuple[DeltaObjectReceipt, ...],
    recipe: SearchViewRecipe,
    parent_total_chunks: int,
    after_total_chunks: int,
    token_delta: dict[str, list[int]],
    new_posting_tokens: set[str],
    check_cancelled: Callable[[], None],
) -> tuple[
    tuple[TokenContribution, ...],
    int,
    int,
    int,
    tuple[TokenContribution, ...],
]:
    ordered_tokens = tuple(
        sorted(touched_tokens, key=lambda token: token.encode("utf-8"))
    )
    if not ordered_tokens:
        return (), 0, 0, 0, ()
    parent_values = [[0, 0, 0] for _token in ordered_tokens]
    windows_read = 0
    posting_bytes_read = 0
    layers: tuple[PostingLayerReceipt, ...] = (
        base.layer,
        *(delta.layer for delta in deltas),
    )
    worker_count = min(_MAX_PARENT_LOOKUP_WORKERS, len(layers))
    parallel_lookup = worker_count > 1
    lookup_aborted = threading.Event()

    def lookup_layer(
        layer: PostingLayerReceipt,
    ) -> tuple[tuple[tuple[int, tuple[int, int, int]], ...], int, int]:
        local_posting_bytes = 0

        def observed(name: str, _offset: int, size: int) -> None:
            nonlocal local_posting_bytes
            if lookup_aborted.is_set():
                raise _ParentLookupAborted("parent lookup aborted")
            if not parallel_lookup:
                check_cancelled()
            if name == "postings.piv":
                local_posting_bytes = _checked_u64_add(
                    "base_posting_bytes_read",
                    local_posting_bytes,
                    size,
                )

        with PostingLayerReader(
            layer,
            recipe=recipe,
            read_observer=observed,
            load_documents=False,
            verify_term_canonical=False,
        ) as reader:
            records = reader.lookup_terms(ordered_tokens)
            sparse = tuple(
                (ordinal, record.delta)
                for ordinal, token in enumerate(ordered_tokens)
                if (record := records[token]) is not None
            )
            return (
                sparse,
                reader.last_sparse_windows_read,
                local_posting_bytes,
            )

    def merge_layer(
        result: tuple[
            tuple[tuple[int, tuple[int, int, int]], ...],
            int,
            int,
        ],
    ) -> None:
        nonlocal windows_read, posting_bytes_read
        sparse, layer_windows, layer_posting_bytes = result
        windows_read = _checked_u64_add(
            "parent_term_windows_read",
            windows_read,
            layer_windows,
        )
        posting_bytes_read = _checked_u64_add(
            "base_posting_bytes_read",
            posting_bytes_read,
            layer_posting_bytes,
        )
        for ordinal, deltas_for_token in sparse:
            token = ordered_tokens[ordinal]
            values = parent_values[ordinal]
            for position, delta in enumerate(deltas_for_token):
                values[position] = _checked_signed_add(
                    f"{token!r} parent DF",
                    values[position],
                    delta,
                )

    if worker_count <= 1:
        for layer in layers:
            check_cancelled()
            merge_layer(lookup_layer(layer))
    else:
        executor = ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="piv3-parent-lookup",
        )
        try:
            for start in range(0, len(layers), worker_count):
                check_cancelled()
                futures = tuple(
                    executor.submit(lookup_layer, layer)
                    for layer in layers[start : start + worker_count]
                )
                for future in futures:
                    while not future.done():
                        check_cancelled()
                        wait(
                            (future,),
                            timeout=_PARENT_LOOKUP_CANCEL_POLL_SECONDS,
                        )
                    check_cancelled()
                    result = future.result()
                    merge_layer(result)
        except BaseException:
            lookup_aborted.set()
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    parent_dfs = tuple(
        TokenContribution(token, *parent_values[ordinal])
        for ordinal, token in enumerate(ordered_tokens)
    )
    contributions: list[TokenContribution] = []
    token_count_delta = 0
    for ordinal, token in enumerate(ordered_tokens):
        check_cancelled()
        before = tuple(parent_values[ordinal])
        _validate_df_triple(token, before, parent_total_chunks, "parent")
        change = tuple(token_delta[token])
        after = tuple(before[index] + change[index] for index in range(3))
        _validate_df_triple(token, after, after_total_chunks, "target")
        token_count_delta += int(after[0] > 0) - int(before[0] > 0)
        if change == (0, 0, 0) and token not in new_posting_tokens:
            raise ValueError(
                f"{token!r} has zero DF delta without new Delta postings"
            )
        contributions.append(TokenContribution(token, *change))
    _signed_u64(token_count_delta, "token_count_delta")
    return (
        tuple(contributions),
        token_count_delta,
        windows_read,
        posting_bytes_read,
        parent_dfs,
    )
def _layer_bytes(layer: PostingLayerReceipt) -> int:
    total = 0
    for reference in (
        layer.documents,
        layer.postings,
        layer.chunks,
        layer.terms,
        layer.sparse_index,
    ):
        total = _checked_u64_add("layer bytes", total, reference.byte_size)
    return total


def _delta_bytes(delta: DeltaObjectReceipt) -> int:
    return _checked_u64_add(
        "Delta bytes", delta.manifest_ref.byte_size, _layer_bytes(delta.layer)
    )


def _compaction_recommendation(
    base: BaseObjectReceipt,
    deltas: tuple[DeltaObjectReceipt, ...],
    policy: CompactionPolicy,
) -> CompactionRecommendation:
    base_bytes = _checked_u64_add(
        "Base bytes", base.manifest_ref.byte_size, _layer_bytes(base.layer)
    )
    delta_bytes = 0
    for delta in deltas:
        delta_bytes = _checked_u64_add(
            "Delta bytes", delta_bytes, _delta_bytes(delta)
        )
    layer_limit = len(deltas) >= policy.max_delta_layers
    byte_ratio = (
        delta_bytes * policy.max_delta_bytes_denominator
        >= base_bytes * policy.max_delta_bytes_numerator
    )
    return CompactionRecommendation(
        recommended=layer_limit or byte_ratio,
        layer_limit_reached=layer_limit,
        byte_ratio_reached=byte_ratio,
        delta_layers=len(deltas),
        base_bytes=base_bytes,
        delta_bytes=delta_bytes,
    )


def _artifact_bytes_written(
    delta: DeltaObjectReceipt,
    view: SearchViewReceipt,
) -> int:
    total = _delta_bytes(delta)
    for reference in (
        view.manifest_ref,
        view.statistics_ref,
        view.documents_ref,
    ):
        total = _checked_u64_add("bytes_written", total, reference.byte_size)
    return total


def _remove_scratch(path: Path, primary: BaseException | None) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        if primary is None:
            raise
        add_note = getattr(primary, "add_note", None)
        if callable(add_note):
            add_note(f"cleaning Delta build scratch also failed: {exc}")


def _iter_view_documents(
    owners: dict[str, ViewDocumentOwner],
    check_cancelled: Callable[[], None],
) -> Iterator[tuple[str, ViewDocumentOwner]]:
    for doc_uid in sorted(owners, key=lambda value: value.encode("utf-8")):
        check_cancelled()
        yield doc_uid, owners[doc_uid]


def build_delta_view(
    pageindex_dir: Path,
    parent: SearchViewReceipt,
    generation: LogicalGenerationReceipt,
    generation_recipe: GenerationRecipe,
    changes: SegmentChangeSet,
    new_refs: Iterable[StoredSegmentRef],
    search_view_recipe: SearchViewRecipe | None = None,
    compaction_policy: CompactionPolicy | None = None,
    *,
    max_run_bytes: int = 64 * 1024 * 1024,
    merge_fan_in: int = 32,
    check_cancelled: Callable[[], None] | None = None,
) -> DeltaBuildResult:
    """Build one immutable replacement Delta and its incremental Search View."""

    root = Path(pageindex_dir)
    if not isinstance(parent, SearchViewReceipt):
        raise TypeError("parent must be a SearchViewReceipt")
    if not isinstance(generation, LogicalGenerationReceipt):
        raise TypeError("generation must be a LogicalGenerationReceipt")
    if not isinstance(generation_recipe, GenerationRecipe):
        raise TypeError("generation_recipe must be a GenerationRecipe")
    if not isinstance(changes, SegmentChangeSet):
        raise TypeError("changes must be a SegmentChangeSet")
    if not (changes.added or changes.changed or changes.deleted):
        raise ValueError("incremental Delta requires at least one changed document")
    recipe = SearchViewRecipe() if search_view_recipe is None else search_view_recipe
    if not isinstance(recipe, SearchViewRecipe):
        raise TypeError("search_view_recipe must be a SearchViewRecipe")
    policy = CompactionPolicy() if compaction_policy is None else compaction_policy
    if not isinstance(policy, CompactionPolicy):
        raise TypeError("compaction_policy must be a CompactionPolicy")
    if check_cancelled is None:
        cancel = lambda: None
    elif callable(check_cancelled):
        cancel = check_cancelled
    else:
        raise TypeError("check_cancelled must be callable")
    if isinstance(max_run_bytes, bool) or not isinstance(max_run_bytes, int):
        raise TypeError("max_run_bytes must be an integer")
    if max_run_bytes < 1:
        raise ValueError("max_run_bytes must be positive")
    if isinstance(merge_fan_in, bool) or not isinstance(merge_fan_in, int):
        raise TypeError("merge_fan_in must be an integer")
    if merge_fan_in < 2:
        raise ValueError("merge_fan_in must be at least two")

    cancel()
    new_by_doc = _validate_new_refs(new_refs, changes)
    for doc_key in changes.unchanged:
        if (
            changes.base_by_doc[doc_key].content_hash
            != changes.current_fingerprints[doc_key]
        ):
            raise ValueError(
                f"unchanged document fingerprint differs from its Base ref: {doc_key}"
            )
    authenticated, base, parent_owners, parent_deltas = _authenticate_parent(
        root, parent, changes, cancel
    )
    recipe_hash = canonical_hash(recipe.as_dict())
    if authenticated.search_view_recipe_hash != recipe_hash:
        raise ValueError("SearchViewRecipe does not match the parent View")
    if base.search_view_recipe_hash != recipe_hash:
        raise ValueError("SearchViewRecipe does not match the parent Base")
    parent_statistics = load_view_statistics(authenticated)

    target_refs = (
        new_by_doc[doc_key]
        if doc_key in new_by_doc
        else changes.base_by_doc[doc_key]
        for doc_key in changes.current_fingerprints
    )
    validate_logical_generation_inputs(
        target_refs,
        generation,
        generation_recipe,
        check_cancelled=cancel,
    )
    del target_refs
    if generation.generation_id == authenticated.generation:
        raise ValueError("incremental target Generation must advance")
    cancel()

    root.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(dir=root, prefix=".piv3-delta-build."))
    delta_candidate_dir = scratch / "delta"
    view_candidate_dir = scratch / "view"
    primary: BaseException | None = None
    retain_scratch = False
    try:
        projector = SegmentProjector(root)
        scalar_delta = {field: 0 for field in _SCALAR_FIELDS}
        token_delta: dict[str, list[int]] = {}
        touched_tokens: set[str] = set()
        new_posting_tokens: set[str] = set()
        replacements: list[DocumentReplacement] = []
        old_summaries_loaded = 0
        new_segments_loaded = 0
        new_summaries_built = 0
        projected_postings = 0

        dirty = sorted(
            (*changes.added, *changes.changed, *changes.deleted),
            key=lambda doc_key: make_doc_uid(doc_key).encode("utf-8"),
        )
        with StagedLayerBuilder(
            delta_candidate_dir,
            layer_kind="delta",
            recipe=recipe,
            max_run_bytes=max_run_bytes,
            merge_fan_in=merge_fan_in,
            check_cancelled=cancel,
        ) as stage:
            for doc_key in dirty:
                cancel()
                doc_uid = make_doc_uid(doc_key)
                old_ref = changes.base_by_doc.get(doc_key)
                owner = parent_owners.get(doc_uid)
                if old_ref is not None:
                    if owner is None or owner.doc_key != doc_key:
                        raise ValueError(f"missing parent owner for {doc_key}")
                    old_summary = load_summary(
                        root,
                        old_ref,
                        _old_summary_ref(old_ref, doc_uid, owner),
                    )
                    old_summaries_loaded = _checked_u64_add(
                        "old_summaries_loaded", old_summaries_loaded, 1
                    )
                    _accumulate_summary(
                        old_summary,
                        -1,
                        scalar_delta,
                        token_delta,
                        touched_tokens,
                        new_posting_tokens,
                    )
                    del old_summary

                new_ref = new_by_doc.get(doc_key)
                new_summary = None
                new_summary_ref = None
                new_ordinal = None
                if new_ref is not None:
                    ticket = stage.begin_document(
                        doc_key, doc_uid, new_ref.segment_hash
                    )
                    new_ordinal = ticket.ordinal
                    new_summary, metrics = projector.project_to_sink(
                        new_ref, ticket.add_posting
                    )
                    new_segments_loaded = _checked_u64_add(
                        "new_segments_loaded", new_segments_loaded, 1
                    )
                    new_summaries_built = _checked_u64_add(
                        "new_summaries_built", new_summaries_built, 1
                    )
                    projected_postings = _checked_u64_add(
                        "projected_postings",
                        projected_postings,
                        new_summary.posting_count,
                    )
                    cancel()
                    new_summary_ref = put_summary(root, new_summary)
                    ticket.commit(new_summary.chunk_count, iter(metrics))
                    _accumulate_summary(
                        new_summary,
                        1,
                        scalar_delta,
                        token_delta,
                        touched_tokens,
                        new_posting_tokens,
                    )
                    del ticket, metrics

                replacements.append(
                    DocumentReplacement(
                        doc_key=doc_key,
                        doc_uid=doc_uid,
                        old_segment_hash=(
                            old_ref.segment_hash if old_ref is not None else None
                        ),
                        old_summary_sha256=(
                            owner.summary_sha256 if owner is not None else None
                        ),
                        old_summary_bytes=(
                            owner.summary_bytes if owner is not None else None
                        ),
                        new_segment_hash=(
                            new_ref.segment_hash if new_ref is not None else None
                        ),
                        new_summary_sha256=(
                            new_summary_ref.summary_sha256
                            if new_summary_ref is not None
                            else None
                        ),
                        new_summary_bytes=(
                            new_summary_ref.byte_size
                            if new_summary_ref is not None
                            else None
                        ),
                        new_doc_ordinal=new_ordinal,
                    )
                )
                del new_summary, new_summary_ref
                cancel()

            after_total_chunks = parent_statistics.total_chunks + scalar_delta[
                "total_chunks"
            ]
            _u64(after_total_chunks, "target total_chunks")
            (
                contributions,
                token_count_delta,
                parent_windows_read,
                base_posting_bytes_read,
                parent_dfs,
            ) = _resolve_parent_terms(
                touched_tokens,
                base,
                parent_deltas,
                recipe,
                parent_statistics.total_chunks,
                after_total_chunks,
                token_delta,
                new_posting_tokens,
                cancel,
            )
            expected_document_delta = len(changes.added) - len(changes.deleted)
            if scalar_delta["documents"] != expected_document_delta:
                raise AssertionError(
                    "summary document delta differs from replacement states"
                )
            statistics_delta = StatisticsDelta(
                documents=scalar_delta["documents"],
                total_chunks=scalar_delta["total_chunks"],
                token_count=token_count_delta,
                title_length_sum=scalar_delta["title_length_sum"],
                breadcrumb_length_sum=scalar_delta["breadcrumb_length_sum"],
                body_length_sum=scalar_delta["body_length_sum"],
                posting_count=scalar_delta["posting_count"],
            )
            statistics = statistics_delta.apply(parent_statistics)
            if statistics.documents != generation.document_count:
                raise ValueError(
                    "patched statistics document count differs from target Generation"
                )
            layer = stage.finish(token_contributions=contributions)

        replacement_tuple = tuple(replacements)
        if layer.document_count != len(changes.added) + len(changes.changed):
            raise ValueError("Delta layer document count differs from replacements")
        if layer.term_count != len(touched_tokens):
            raise ValueError("Delta layer term count differs from touched tokens")
        cancel()
        delta_candidate = write_delta_candidate(
            delta_candidate_dir,
            parent=authenticated,
            generation=generation,
            recipe=recipe,
            layer=layer,
            statistics_delta=statistics_delta,
            replacements=replacement_tuple,
        )
        delta = finalize_delta_object(root, delta_candidate)
        cancel()

        for replacement in replacement_tuple:
            cancel()
            if replacement.new_segment_hash is None:
                parent_owners.pop(replacement.doc_uid)
                continue
            if (
                replacement.new_summary_sha256 is None
                or replacement.new_summary_bytes is None
                or replacement.new_doc_ordinal is None
            ):
                raise AssertionError("new replacement lost its complete receipt")
            parent_owners[replacement.doc_uid] = ViewDocumentOwner(
                doc_key=replacement.doc_key,
                segment_hash=replacement.new_segment_hash,
                summary_sha256=replacement.new_summary_sha256,
                summary_bytes=replacement.new_summary_bytes,
                owner_layer_kind="delta",
                owner_layer_id=delta.delta_id,
                doc_ordinal=replacement.new_doc_ordinal,
            )

        view_candidate = write_search_view_candidate(
            view_candidate_dir,
            generation=generation,
            recipe=recipe,
            base=base,
            statistics=statistics,
            documents=_iter_view_documents(parent_owners, cancel),
            delta_ids=authenticated.delta_ids + (delta.delta_id,),
            parent=authenticated,
        )
        del parent_owners
        all_deltas = parent_deltas + (delta,)
        compaction = _compaction_recommendation(base, all_deltas, policy)
        work = DeltaBuildWork(
            old_summaries_loaded=old_summaries_loaded,
            old_segments_loaded=0,
            new_segments_loaded=new_segments_loaded,
            new_summaries_built=new_summaries_built,
            projected_postings=projected_postings,
            touched_tokens=len(touched_tokens),
            parent_term_windows_read=parent_windows_read,
            base_posting_bytes_read=base_posting_bytes_read,
            bytes_written=_artifact_bytes_written(delta, view_candidate),
            layer_count=1 + len(all_deltas),
            segments_loaded_peak=1 if new_segments_loaded else 0,
        )
        witness = ParentDfWitness(
            parent_view_id=authenticated.view_id,
            parent_view_manifest_sha256=authenticated.manifest_ref.sha256,
            search_view_recipe_hash=authenticated.search_view_recipe_hash,
            parent_total_chunks=parent_statistics.total_chunks,
            contributions=parent_dfs,
        )
        candidate_result = DeltaBuildResult(
            delta,
            view_candidate,
            compaction,
            work,
            parent_df_witness=witness,
        )
        cancel()
        view = finalize_search_view(root, view_candidate)
        return replace(candidate_result, view=view)
    except BaseException as exc:
        primary = exc
        retain_scratch = isinstance(
            exc, (DeltaStoreConflictError, ViewStoreConflictError)
        )
        raise
    finally:
        if not retain_scratch:
            _remove_scratch(scratch, primary)


__all__ = [
    "CompactionRecommendation",
    "DeltaBuildResult",
    "DeltaBuildWork",
    "build_delta_view",
]
