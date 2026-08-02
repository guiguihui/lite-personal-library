"""Independent bounded Normal validation for PageIndex v3 objects."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
from pathlib import Path
from typing import Any

from app.index.v2.canonical import canonical_bytes, canonical_hash
from app.index.v2.object_store import StoredSegmentRef
from app.index.v2.streaming_json import load_bounded_canonical_json
from app.index.v2.validator import ValidationReport

from .delta_store import (
    DeltaObjectReceipt,
    StatisticsDelta,
    load_delta_object_metadata,
)
from .generation import LogicalGenerationReceipt
from .generation_stream import validate_generation_stream
from .layer_codec import PostingLayerReader
from .models import (
    ChunkRef,
    SearchViewRecipe,
    ViewPin,
    make_doc_uid,
)
from .segment_projection import SegmentProjector
from .statistics import CorpusTotals
from .summary_store import StoredSummaryRef, load_summary
from .view_store import (
    BaseObjectReceipt,
    SearchViewReceipt,
    ViewDocumentOwner,
    load_base_object_metadata,
    load_search_view_metadata,
    load_view_documents,
    load_view_statistics,
)


class _CallbackRaised(BaseException):
    """Keep callback failures outside the ValidationReport error boundary."""

    def __init__(self, error: BaseException) -> None:
        super().__init__(str(error))
        self.error = error


def _check_cancelled(
    callback: Callable[[], None] | None,
) -> Callable[[], None]:
    if callback is None:
        return lambda: None
    if not callable(callback):
        raise TypeError("check_cancelled must be callable")

    def check() -> None:
        try:
            callback()
        except BaseException as exc:
            raise _CallbackRaised(exc) from exc

    return check


def _capture(code: str, validate: Callable[[], None]) -> ValidationReport:
    try:
        validate()
    except _CallbackRaised as exc:
        raise exc.error
    except Exception as exc:
        return ValidationReport(False, (f"{code}: {exc}",))
    return ValidationReport(True)


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{field} keys must be strings")
    return value


def _validate_generation(
    receipt: LogicalGenerationReceipt,
    pageindex_dir: Path,
    check_cancelled: Callable[[], None],
    *,
    collect_refs: bool = True,
) -> dict[str, StoredSegmentRef]:
    """Prove the supplied candidate is canonical and internally self-bound.

    This Task 8 boundary deliberately has no external source pin. Therefore it
    can distinguish corruption or semantic rebinding inside this candidate,
    but cannot distinguish it from a different, wholly valid Generation whose
    manifest, proof, and receipt were all supplied together. That upstream
    source anchor belongs to the later orchestration boundary.
    """

    return validate_generation_stream(
        receipt,
        pageindex_dir,
        check_cancelled=check_cancelled,
        collect_refs=collect_refs,
    )


def _validate_base(
    receipt: BaseObjectReceipt,
    generation: LogicalGenerationReceipt,
    pageindex_dir: Path,
    check_cancelled: Callable[[], None],
) -> None:
    if not isinstance(receipt, BaseObjectReceipt):
        raise TypeError("receipt must be a BaseObjectReceipt")
    if not isinstance(generation, LogicalGenerationReceipt):
        raise TypeError("generation must be a LogicalGenerationReceipt")

    generation_refs = _validate_generation(
        generation, pageindex_dir, check_cancelled
    )
    check_cancelled()
    authenticated = load_base_object_metadata(pageindex_dir, receipt.base_id)
    if authenticated.attestation_dict() != receipt.attestation_dict():
        raise ValueError("Base receipt differs from authenticated manifest")
    if authenticated.root.resolve() != receipt.root.resolve():
        raise ValueError("Base receipt root differs from authenticated object")
    if authenticated.generation != generation.generation_id:
        raise ValueError("Base generation differs from logical Generation")
    if (
        authenticated.generation_manifest_sha256
        != generation.manifest_ref.sha256
    ):
        raise ValueError("Base Generation manifest binding is invalid")

    manifest = _mapping(
        load_bounded_canonical_json(
            authenticated.root / authenticated.manifest_ref.relative_path
        ),
        "Base manifest",
    )
    recipe_value = _mapping(
        manifest["search_view_recipe"], "search_view_recipe"
    )
    try:
        recipe = SearchViewRecipe(**dict(recipe_value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid search_view_recipe: {exc}") from exc
    if recipe.as_dict() != dict(recipe_value):
        raise ValueError("search_view_recipe is not normalized")
    if canonical_hash(recipe.as_dict()) != authenticated.search_view_recipe_hash:
        raise ValueError("Base search_view_recipe hash mismatch")

    documents = 0
    total_chunks = 0
    title_length_sum = 0
    breadcrumb_length_sum = 0
    body_length_sum = 0
    token_count = 0
    posting_count = 0
    remaining = generation_refs

    check_cancelled()
    with PostingLayerReader(authenticated.layer, recipe=recipe) as reader:
        # Base Normal is the explicit full-object path: audit first, then
        # independently recompute scalar totals. Dirty Delta validation never
        # enters this path or scans the Base vocabulary/posting artifacts.
        reader.audit()
        check_cancelled()
        for ordinal, document in enumerate(reader._documents):
            check_cancelled()
            ref = remaining.pop(document.doc_key, None)
            if ref is None:
                raise ValueError(
                    f"Base layer document is absent from Generation: "
                    f"{document.doc_key}"
                )
            if ref.segment_hash != document.segment_hash:
                raise ValueError(
                    f"Base layer Segment differs from Generation: "
                    f"{document.doc_key}"
                )
            metrics = reader.get_chunk_metrics(
                ChunkRef(
                    document.doc_uid,
                    document.segment_hash,
                    local_id,
                )
                for local_id in range(document.chunk_count)
            )
            if len(metrics) != document.chunk_count:
                raise ValueError(
                    f"Base PCV metric count mismatch for ordinal {ordinal}"
                )
            documents += 1
            total_chunks += document.chunk_count
            for metric in metrics.values():
                title_length_sum += metric.title_length
                breadcrumb_length_sum += metric.breadcrumb_length
                body_length_sum += metric.body_length
            del metrics
            check_cancelled()

        if remaining:
            raise ValueError(
                "Base layer omits Generation documents: "
                + ", ".join(sorted(remaining))
            )

        reader._reset_sparse_work()
        for window_index in range(len(reader._windows)):
            check_cancelled()
            for _encoded, record in reader._decode_sparse_window(window_index):
                token_count += 1
                posting_count += record.df_any_delta
            check_cancelled()

    observed = CorpusTotals(
        documents=documents,
        total_chunks=total_chunks,
        token_count=token_count,
        title_length_sum=title_length_sum,
        breadcrumb_length_sum=breadcrumb_length_sum,
        body_length_sum=body_length_sum,
        posting_count=posting_count,
    )
    if observed != authenticated.statistics:
        raise ValueError(
            "Base statistics differ from independently decoded layer totals"
        )
    check_cancelled()


def _authenticated_view(
    receipt: SearchViewReceipt,
    pageindex_dir: Path,
) -> SearchViewReceipt:
    if not isinstance(receipt, SearchViewReceipt):
        raise TypeError("View receipt must be a SearchViewReceipt")
    authenticated = load_search_view_metadata(pageindex_dir, receipt.view_id)
    if authenticated.attestation_dict() != receipt.attestation_dict():
        raise ValueError("View receipt differs from authenticated manifest")
    if authenticated.root.resolve() != receipt.root.resolve():
        raise ValueError("View receipt root differs from authenticated object")
    return authenticated


def _authenticated_delta(
    receipt: DeltaObjectReceipt,
    pageindex_dir: Path,
) -> DeltaObjectReceipt:
    if not isinstance(receipt, DeltaObjectReceipt):
        raise TypeError("receipt must be a DeltaObjectReceipt")
    authenticated = load_delta_object_metadata(pageindex_dir, receipt.delta_id)
    if authenticated.attestation_dict() != receipt.attestation_dict():
        raise ValueError("Delta receipt differs from authenticated manifest")
    if authenticated.root.resolve() != receipt.root.resolve():
        raise ValueError("Delta receipt root differs from authenticated object")
    return authenticated


def _search_recipe(root: Path) -> SearchViewRecipe:
    manifest = _mapping(
        load_bounded_canonical_json(root / "manifest.json"),
        "search object manifest",
    )
    raw = _mapping(manifest["search_view_recipe"], "search_view_recipe")
    try:
        recipe = SearchViewRecipe(**dict(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid search_view_recipe: {exc}") from exc
    if recipe.as_dict() != dict(raw):
        raise ValueError("search_view_recipe is not normalized")
    return recipe


def _load_view_chain(
    view: SearchViewReceipt,
    pageindex_dir: Path,
    check_cancelled: Callable[[], None],
) -> tuple[
    BaseObjectReceipt,
    tuple[DeltaObjectReceipt, ...],
    tuple[SearchViewReceipt, ...],
]:
    check_cancelled()
    base = load_base_object_metadata(pageindex_dir, view.base_id)
    if base.search_view_recipe_hash != view.search_view_recipe_hash:
        raise ValueError("View chain changes SearchViewRecipe at its Base")
    expected_generation = base.generation
    expected_manifest = base.generation_manifest_sha256
    prefix: tuple[str, ...] = ()
    deltas: list[DeltaObjectReceipt] = []
    parents: list[SearchViewReceipt] = []
    seen_views = {view.view_id}
    for delta_id in view.delta_ids:
        check_cancelled()
        delta = load_delta_object_metadata(pageindex_dir, delta_id)
        declared_parent = load_search_view_metadata(
            pageindex_dir, delta.parent_view_id
        )
        if declared_parent.view_id in seen_views:
            raise ValueError("View chain contains a cycle")
        seen_views.add(declared_parent.view_id)
        if delta.parent_view_manifest_sha256 != declared_parent.manifest_ref.sha256:
            raise ValueError("Delta is rebound from its declared parent View")
        if declared_parent.base_id != view.base_id:
            raise ValueError("View chain changes Base identity")
        if declared_parent.delta_ids != prefix:
            raise ValueError("View Delta chain is reordered or non-contiguous")
        if declared_parent.search_view_recipe_hash != view.search_view_recipe_hash:
            raise ValueError("View chain changes SearchViewRecipe")
        if (
            declared_parent.generation != expected_generation
            or declared_parent.generation_manifest_sha256 != expected_manifest
        ):
            raise ValueError("declared parent View has an invalid Generation boundary")
        if delta.search_view_recipe_hash != view.search_view_recipe_hash:
            raise ValueError("Delta chain changes SearchViewRecipe")
        if delta.generation == declared_parent.generation:
            raise ValueError("Delta target Generation does not advance")
        deltas.append(delta)
        parents.append(declared_parent)
        prefix += (delta.delta_id,)
        expected_generation = delta.generation
        expected_manifest = delta.generation_manifest_sha256
    if prefix != view.delta_ids:
        raise ValueError("View Delta chain differs from its declared order")
    if (
        view.generation != expected_generation
        or view.generation_manifest_sha256 != expected_manifest
    ):
        raise ValueError("View Generation does not match its Delta chain")
    check_cancelled()
    return base, tuple(deltas), tuple(parents)


def _summary_ref(
    ref: StoredSegmentRef,
    summary_sha256: str,
    summary_bytes: int,
) -> StoredSummaryRef:
    return StoredSummaryRef(
        segment_hash=ref.segment_hash,
        summary_sha256=summary_sha256,
        byte_size=summary_bytes,
        doc_key=ref.doc_key,
        doc_uid=make_doc_uid(ref.doc_key),
        content_hash=ref.content_hash,
        segment_recipe_hash=ref.segment_recipe_hash,
    )


def _accumulate_summary(
    summary: object,
    sign: int,
    scalars: dict[str, int],
    token_delta: dict[str, list[int]],
    new_token_df: dict[str, list[int]] | None = None,
) -> None:
    scalar_values = {
        "documents": 1,
        "total_chunks": getattr(summary, "chunk_count"),
        "title_length_sum": getattr(summary, "title_length_sum"),
        "breadcrumb_length_sum": getattr(summary, "breadcrumb_length_sum"),
        "body_length_sum": getattr(summary, "body_length_sum"),
        "posting_count": getattr(summary, "posting_count"),
    }
    for field, value in scalar_values.items():
        scalars[field] += sign * value
    for token in getattr(summary, "tokens"):
        values = token_delta.setdefault(token.token, [0, 0, 0])
        triple = (token.df_any, token.df_nonbody, token.df_body)
        for position, value in enumerate(triple):
            values[position] += sign * value
        if new_token_df is not None:
            new_values = new_token_df.setdefault(token.token, [0, 0, 0])
            for position, value in enumerate(triple):
                new_values[position] += value


def _update_posting_digest(digest: Any, posting: Any) -> None:
    ref = posting.chunk_ref
    digest.update(
        canonical_bytes(
            [
                posting.token,
                ref.doc_uid,
                ref.segment_hash,
                ref.local_id,
                posting.title_tf,
                posting.breadcrumb_tf,
                posting.body_tf,
            ]
        )
    )
    digest.update(b"\n")


def _validate_df(
    token: str,
    triple: tuple[int, int, int],
    total_chunks: int,
    state: str,
) -> None:
    any_df, nonbody_df, body_df = triple
    if min(triple) < 0 or max(triple) > total_chunks:
        raise ValueError(f"{token!r} {state} DF is outside corpus bounds")
    if max(nonbody_df, body_df) > any_df or any_df > nonbody_df + body_df:
        raise ValueError(f"{token!r} {state} DF union is invalid")


def _validate_delta(
    receipt: DeltaObjectReceipt,
    parent: SearchViewReceipt,
    target: SearchViewReceipt,
    parent_generation: LogicalGenerationReceipt,
    generation: LogicalGenerationReceipt,
    parent_pin: ViewPin,
    target_pin: ViewPin,
    pageindex_dir: Path,
    check_cancelled: Callable[[], None],
) -> None:
    check_cancelled()
    if not isinstance(parent_generation, LogicalGenerationReceipt):
        raise TypeError("parent_generation must be a LogicalGenerationReceipt")
    if not isinstance(generation, LogicalGenerationReceipt):
        raise TypeError("generation must be a LogicalGenerationReceipt")
    if not isinstance(parent_pin, ViewPin) or not isinstance(target_pin, ViewPin):
        raise TypeError("parent_pin and target_pin must be ViewPin values")
    if (
        parent_pin.generation != parent_generation.generation_id
        or parent_pin.view_id != parent.view_id
    ):
        raise ValueError("parent View differs from trusted parent pin")
    if (
        target_pin.generation != generation.generation_id
        or target_pin.view_id != target.view_id
    ):
        raise ValueError("target View differs from trusted target pin")
    parent_refs = _validate_generation(
        parent_generation, pageindex_dir, check_cancelled
    )
    target_refs = _validate_generation(
        generation, pageindex_dir, check_cancelled
    )
    authenticated_parent = _authenticated_view(parent, pageindex_dir)
    authenticated_target = _authenticated_view(target, pageindex_dir)
    authenticated_delta = _authenticated_delta(receipt, pageindex_dir)
    check_cancelled()

    if (
        authenticated_parent.generation != parent_generation.generation_id
        or authenticated_parent.generation_manifest_sha256
        != parent_generation.manifest_ref.sha256
    ):
        raise ValueError("parent View does not bind parent_generation")
    if (
        authenticated_target.generation != generation.generation_id
        or authenticated_target.generation_manifest_sha256
        != generation.manifest_ref.sha256
    ):
        raise ValueError("target View does not bind target Generation")
    if authenticated_delta.parent_view_id != authenticated_parent.view_id:
        raise ValueError("Delta parent_view_id differs from parent View")
    if (
        authenticated_delta.parent_view_manifest_sha256
        != authenticated_parent.manifest_ref.sha256
    ):
        raise ValueError("Delta parent View manifest binding is invalid")
    if authenticated_delta.generation != generation.generation_id:
        raise ValueError("Delta target Generation identity is invalid")
    if (
        authenticated_delta.generation_manifest_sha256
        != generation.manifest_ref.sha256
    ):
        raise ValueError("Delta target Generation manifest binding is invalid")
    if authenticated_target.base_id != authenticated_parent.base_id:
        raise ValueError("incremental target changes Base identity")
    if authenticated_target.delta_ids != (
        authenticated_parent.delta_ids + (authenticated_delta.delta_id,)
    ):
        raise ValueError("target View does not append exactly this Delta")

    base, parent_deltas, _ = _load_view_chain(
        authenticated_parent, pageindex_dir, check_cancelled
    )
    if base.base_id != authenticated_parent.base_id:
        raise ValueError("authenticated parent chain has the wrong Base")
    recipe = _search_recipe(authenticated_delta.root)
    recipe_hash = canonical_hash(recipe.as_dict())
    if recipe_hash != authenticated_delta.search_view_recipe_hash:
        raise ValueError("Delta search_view_recipe hash mismatch")
    for item in (base, authenticated_parent, authenticated_target):
        if item.search_view_recipe_hash != recipe_hash:
            raise ValueError("incremental objects use different SearchViewRecipes")

    parent_statistics = load_view_statistics(authenticated_parent)
    target_statistics = load_view_statistics(authenticated_target)
    parent_owners = load_view_documents(authenticated_parent)
    target_owners = load_view_documents(authenticated_target)
    if len(parent_owners) != len(parent_refs):
        raise ValueError("parent owner count differs from parent Generation")
    for doc_uid, owner in parent_owners.items():
        check_cancelled()
        ref = parent_refs.get(owner.doc_key)
        if (
            doc_uid != make_doc_uid(owner.doc_key)
            or ref is None
            or owner.segment_hash != ref.segment_hash
        ):
            raise ValueError("parent owner map differs from parent Generation")

    all_doc_keys = set(parent_refs) | set(target_refs)
    expected_dirty = {
        doc_key
        for doc_key in all_doc_keys
        if (
            parent_refs.get(doc_key) is None
            or target_refs.get(doc_key) is None
            or parent_refs[doc_key].segment_hash
            != target_refs[doc_key].segment_hash
        )
    }
    replacement_keys = {item.doc_key for item in authenticated_delta.replacements}
    if replacement_keys != expected_dirty:
        raise ValueError("Delta replacements do not exactly cover changed documents")

    scalars = {
        "documents": 0,
        "total_chunks": 0,
        "title_length_sum": 0,
        "breadcrumb_length_sum": 0,
        "body_length_sum": 0,
        "posting_count": 0,
    }
    token_delta: dict[str, list[int]] = {}
    new_token_df: dict[str, list[int]] = {}
    projected_metrics_by_uid: dict[str, tuple[object, ...]] = {}
    expected_posting_receipts: dict[str, tuple[str, int]] = {}
    expected_new_replacements = []
    projector = SegmentProjector(pageindex_dir)

    for replacement in authenticated_delta.replacements:
        check_cancelled()
        parent_ref = parent_refs.get(replacement.doc_key)
        target_ref = target_refs.get(replacement.doc_key)
        owner = parent_owners.get(replacement.doc_uid)
        if replacement.old_segment_hash is None:
            if parent_ref is not None or owner is not None:
                raise ValueError("add replacement already exists in parent")
        else:
            if (
                parent_ref is None
                or owner is None
                or replacement.old_segment_hash != parent_ref.segment_hash
                or owner.segment_hash != parent_ref.segment_hash
                or replacement.old_summary_sha256 != owner.summary_sha256
                or replacement.old_summary_bytes != owner.summary_bytes
            ):
                raise ValueError("old replacement does not match parent owner")
            assert replacement.old_summary_sha256 is not None
            assert replacement.old_summary_bytes is not None
            old_summary = load_summary(
                pageindex_dir,
                parent_ref,
                _summary_ref(
                    parent_ref,
                    replacement.old_summary_sha256,
                    replacement.old_summary_bytes,
                ),
            )
            _accumulate_summary(
                old_summary, -1, scalars, token_delta
            )
            del old_summary

        if replacement.new_segment_hash is None:
            if target_ref is not None:
                raise ValueError("delete replacement remains in target Generation")
        else:
            if (
                target_ref is None
                or replacement.new_segment_hash != target_ref.segment_hash
            ):
                raise ValueError("new replacement differs from target Generation")
            assert replacement.new_summary_sha256 is not None
            assert replacement.new_summary_bytes is not None
            assert replacement.new_doc_ordinal is not None
            new_summary = load_summary(
                pageindex_dir,
                target_ref,
                _summary_ref(
                    target_ref,
                    replacement.new_summary_sha256,
                    replacement.new_summary_bytes,
                ),
            )
            expected_digest = hashlib.sha256()
            expected_rows = 0

            def consume_expected(posting: object) -> None:
                nonlocal expected_rows
                if expected_rows % 1024 == 0:
                    check_cancelled()
                _update_posting_digest(expected_digest, posting)
                expected_rows += 1

            projected_summary, projected_metrics = projector.project_to_sink(
                target_ref,
                consume_expected,
            )
            if projected_summary != new_summary:
                raise ValueError(
                    "stored new summary differs from its immutable Segment"
                )
            _accumulate_summary(
                new_summary,
                1,
                scalars,
                token_delta,
                new_token_df,
            )
            projected_metrics_by_uid[replacement.doc_uid] = tuple(
                projected_metrics
            )
            expected_posting_receipts[replacement.doc_uid] = (
                expected_digest.hexdigest(),
                expected_rows,
            )
            expected_new_replacements.append(replacement)
            del new_summary, projected_summary, projected_metrics
        check_cancelled()

    ordered_tokens = sorted(
        token_delta, key=lambda value: value.encode("utf-8")
    )
    observed_tokens: list[str] = []
    check_cancelled()
    with PostingLayerReader(
        authenticated_delta.layer, recipe=recipe
    ) as reader:
        reader.audit()
        check_cancelled()
        if len(reader._documents) != len(expected_new_replacements):
            raise ValueError("Delta document table differs from new replacements")
        for ordinal, (document, replacement) in enumerate(
            zip(
                reader._documents,
                expected_new_replacements,
                strict=True,
            )
        ):
            check_cancelled()
            if (
                replacement.new_doc_ordinal != ordinal
                or document.doc_key != replacement.doc_key
                or document.doc_uid != replacement.doc_uid
                or document.segment_hash != replacement.new_segment_hash
            ):
                raise ValueError(
                    "Delta document table contains a row outside live replacements"
                )
            expected_metrics = projected_metrics_by_uid[document.doc_uid]
            refs = tuple(
                ChunkRef(
                    document.doc_uid,
                    document.segment_hash,
                    local_id,
                )
                for local_id in range(document.chunk_count)
            )
            metrics = reader.get_chunk_metrics(iter(refs))
            observed_metrics = tuple(metrics[ref] for ref in refs)
            if observed_metrics != expected_metrics:
                raise ValueError(
                    "Delta PCV metrics differ from immutable Segment metrics"
                )
            del refs, metrics

        actual_posting_digests = {
            doc_uid: hashlib.sha256()
            for doc_uid in expected_posting_receipts
        }
        actual_posting_counts = {
            doc_uid: 0 for doc_uid in expected_posting_receipts
        }
        actual_rows_seen = 0
        reader._reset_sparse_work()
        for window_index in range(len(reader._windows)):
            check_cancelled()
            for _encoded, record in reader._decode_sparse_window(window_index):
                check_cancelled()
                observed_tokens.append(record.token)
                expected_delta = token_delta.get(record.token)
                if expected_delta is None or record.delta != tuple(expected_delta):
                    raise ValueError(
                        "Delta touched-term contribution differs from summaries"
                    )
                actual_new = [0, 0, 0]
                if record.has_postings:
                    for posting in reader._iter_complete(record):
                        doc_uid = posting.chunk_ref.doc_uid
                        digest = actual_posting_digests.get(doc_uid)
                        if digest is None:
                            raise ValueError(
                                "Delta PIV row is outside live new replacements"
                            )
                        _update_posting_digest(digest, posting)
                        actual_posting_counts[doc_uid] += 1
                        actual_rows_seen += 1
                        if actual_rows_seen % 1024 == 0:
                            check_cancelled()
                        actual_new[0] += 1
                        if posting.title_tf or posting.breadcrumb_tf:
                            actual_new[1] += 1
                        if posting.body_tf:
                            actual_new[2] += 1
                expected_new = new_token_df.get(record.token, [0, 0, 0])
                if actual_new != expected_new:
                    raise ValueError(
                        "Delta physical posting DF differs from new summaries"
                    )
            check_cancelled()
    if observed_tokens != ordered_tokens:
        raise ValueError(
            "Delta term set differs from the changed-summary touched union"
        )
    for doc_uid, (expected_sha256, expected_rows) in (
        expected_posting_receipts.items()
    ):
        check_cancelled()
        if (
            actual_posting_counts[doc_uid] != expected_rows
            or actual_posting_digests[doc_uid].hexdigest()
            != expected_sha256
        ):
            raise ValueError(
                "Delta PIV posting TFs differ from immutable Segment postings"
            )

    parent_values = {token: [0, 0, 0] for token in ordered_tokens}
    parent_layers = (
        base.layer,
        *(item.layer for item in parent_deltas),
    )
    for layer in parent_layers:
        check_cancelled()
        with PostingLayerReader(
            layer,
            recipe=recipe,
            load_documents=False,
        ) as reader:
            records = reader.lookup_terms(ordered_tokens)
            for token in ordered_tokens:
                record = records[token]
                if record is None:
                    continue
                values = parent_values[token]
                for position, value in enumerate(record.delta):
                    values[position] += value

    token_count_delta = 0
    for token in ordered_tokens:
        check_cancelled()
        before = tuple(parent_values[token])
        after = tuple(
            before[position] + token_delta[token][position]
            for position in range(3)
        )
        _validate_df(
            token, before, parent_statistics.total_chunks, "parent"
        )
        _validate_df(
            token, after, target_statistics.total_chunks, "target"
        )
        token_count_delta += int(after[0] > 0) - int(before[0] > 0)

    expected_statistics_delta = StatisticsDelta(
        token_count=token_count_delta,
        **scalars,
    )
    if expected_statistics_delta != authenticated_delta.statistics_delta:
        raise ValueError(
            "Delta statistics_delta differs from changed summaries"
        )
    if expected_statistics_delta.apply(parent_statistics) != target_statistics:
        raise ValueError(
            "target View statistics do not equal parent plus Delta"
        )
    if target_statistics.documents != len(target_refs):
        raise ValueError("target statistics differ from target Generation")

    for replacement in authenticated_delta.replacements:
        check_cancelled()
        if replacement.old_segment_hash is not None:
            removed = parent_owners.pop(replacement.doc_uid, None)
            if (
                removed is None
                or removed.segment_hash != replacement.old_segment_hash
            ):
                raise ValueError("owner patch cannot remove its old owner")
        if replacement.new_segment_hash is not None:
            assert replacement.new_summary_sha256 is not None
            assert replacement.new_summary_bytes is not None
            assert replacement.new_doc_ordinal is not None
            parent_owners[replacement.doc_uid] = ViewDocumentOwner(
                doc_key=replacement.doc_key,
                segment_hash=replacement.new_segment_hash,
                summary_sha256=replacement.new_summary_sha256,
                summary_bytes=replacement.new_summary_bytes,
                owner_layer_kind="delta",
                owner_layer_id=authenticated_delta.delta_id,
                doc_ordinal=replacement.new_doc_ordinal,
            )
    if parent_owners != target_owners:
        raise ValueError("target owner map differs from parent replacement patch")
    if len(target_owners) != len(target_refs):
        raise ValueError("target owner count differs from target Generation")
    for doc_uid, owner in target_owners.items():
        check_cancelled()
        ref = target_refs.get(owner.doc_key)
        if (
            doc_uid != make_doc_uid(owner.doc_key)
            or ref is None
            or owner.segment_hash != ref.segment_hash
        ):
            raise ValueError("target owner map differs from target Generation")
    check_cancelled()


def _validate_view(
    receipt: SearchViewReceipt,
    generation: LogicalGenerationReceipt,
    pin: ViewPin,
    pageindex_dir: Path,
    check_cancelled: Callable[[], None],
) -> None:
    check_cancelled()
    if not isinstance(receipt, SearchViewReceipt):
        raise TypeError("receipt must be a SearchViewReceipt")
    if not isinstance(generation, LogicalGenerationReceipt):
        raise TypeError("generation must be a LogicalGenerationReceipt")
    if not isinstance(pin, ViewPin):
        raise TypeError("pin must be a ViewPin")
    if pin.generation != generation.generation_id:
        raise ValueError("trusted View pin differs from logical Generation")
    if pin.view_id != receipt.view_id:
        raise ValueError("View receipt differs from trusted View pin")
    generation_refs = _validate_generation(
        generation, pageindex_dir, check_cancelled
    )
    authenticated = _authenticated_view(receipt, pageindex_dir)
    if (
        authenticated.generation != generation.generation_id
        or authenticated.generation_manifest_sha256
        != generation.manifest_ref.sha256
    ):
        raise ValueError("View does not bind the requested target Generation")

    base, deltas, declared_parents = _load_view_chain(
        authenticated, pageindex_dir, check_cancelled
    )
    recipe = _search_recipe(authenticated.root)
    recipe_hash = canonical_hash(recipe.as_dict())
    if recipe_hash != authenticated.search_view_recipe_hash:
        raise ValueError("View search_view_recipe hash mismatch")
    if base.search_view_recipe_hash != recipe_hash:
        raise ValueError("View and Base SearchViewRecipes differ")
    for delta in deltas:
        check_cancelled()
        if delta.search_view_recipe_hash != recipe_hash:
            raise ValueError("View Delta chain changes SearchViewRecipe")

    if deltas:
        initial = declared_parents[0]
    else:
        initial = authenticated
    if initial.delta_ids:
        raise ValueError("View replay does not begin at a zero-Delta View")
    if (
        initial.base_id != base.base_id
        or initial.search_view_recipe_hash != recipe_hash
        or initial.generation != base.generation
        or initial.generation_manifest_sha256
        != base.generation_manifest_sha256
    ):
        raise ValueError("initial View does not bind the replay Base")

    replayed_statistics = load_view_statistics(initial)
    if replayed_statistics != base.statistics:
        raise ValueError("initial View statistics differ from Base statistics")
    replayed_owners = load_view_documents(initial)
    if len(replayed_owners) != base.statistics.documents:
        raise ValueError("initial owner count differs from Base statistics")

    prefix: tuple[str, ...] = ()
    for position, delta in enumerate(deltas):
        check_cancelled()
        declared_parent = declared_parents[position]
        if (
            declared_parent.view_id != delta.parent_view_id
            or declared_parent.manifest_ref.sha256
            != delta.parent_view_manifest_sha256
        ):
            raise ValueError("Delta parent View binding is invalid during replay")
        if (
            declared_parent.base_id != base.base_id
            or declared_parent.delta_ids != prefix
        ):
            raise ValueError("Delta parent View is reordered or spliced")
        # Schema v1 owner maps are whole-file hashes. Re-materializing every
        # intermediate map would make Normal validation O(N*D). The signed
        # parent manifests and old-side replacements authenticate the chain;
        # exact intermediate map integrity requires a patch/Merkle schema v2.
        replayed_statistics = delta.statistics_delta.apply(
            replayed_statistics
        )
        for replacement in delta.replacements:
            check_cancelled()
            existing = replayed_owners.get(replacement.doc_uid)
            if replacement.old_segment_hash is None:
                if existing is not None:
                    raise ValueError(
                        "add replacement collides with a replayed owner"
                    )
            else:
                if (
                    existing is None
                    or existing.doc_key != replacement.doc_key
                    or existing.segment_hash
                    != replacement.old_segment_hash
                    or existing.summary_sha256
                    != replacement.old_summary_sha256
                    or existing.summary_bytes
                    != replacement.old_summary_bytes
                ):
                    raise ValueError(
                        "replacement old side differs from replayed owner"
                    )
                replayed_owners.pop(replacement.doc_uid)

            if replacement.new_segment_hash is not None:
                assert replacement.new_summary_sha256 is not None
                assert replacement.new_summary_bytes is not None
                assert replacement.new_doc_ordinal is not None
                new_owner = ViewDocumentOwner(
                    doc_key=replacement.doc_key,
                    segment_hash=replacement.new_segment_hash,
                    summary_sha256=replacement.new_summary_sha256,
                    summary_bytes=replacement.new_summary_bytes,
                    owner_layer_kind="delta",
                    owner_layer_id=delta.delta_id,
                    doc_ordinal=replacement.new_doc_ordinal,
                )
                replayed_owners[replacement.doc_uid] = new_owner
        if replayed_statistics.documents != len(replayed_owners):
            raise ValueError(
                "replayed statistics and owner counts differ"
            )
        prefix += (delta.delta_id,)
        check_cancelled()

    if prefix != authenticated.delta_ids:
        raise ValueError("replayed Delta order differs from target View")
    final_statistics = load_view_statistics(authenticated)
    final_owners = load_view_documents(authenticated)
    if replayed_statistics != final_statistics:
        raise ValueError("final View statistics differ from chain replay")
    if replayed_owners != final_owners:
        raise ValueError("final View owners differ from chain replay")
    if (
        final_statistics.documents != len(generation_refs)
        or len(final_owners) != len(generation_refs)
    ):
        raise ValueError("final View counts differ from target Generation")

    remaining = generation_refs
    for doc_uid, owner in final_owners.items():
        check_cancelled()
        ref = remaining.pop(owner.doc_key, None)
        if (
            doc_uid != make_doc_uid(owner.doc_key)
            or ref is None
            or ref.segment_hash != owner.segment_hash
        ):
            raise ValueError("final View owner differs from target Generation")
    if remaining:
        raise ValueError("final View omits target Generation documents")
    check_cancelled()


def validate_generation_normal(
    receipt: LogicalGenerationReceipt,
    pageindex_dir: Path,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> ValidationReport:
    """Validate one logical Generation without loading or compiling Segments."""

    cancel = _check_cancelled(check_cancelled)
    return _capture(
        "generation_invalid",
        lambda: _validate_generation(
            receipt,
            Path(pageindex_dir),
            cancel,
            collect_refs=False,
        ),
    )


def validate_base_normal(
    receipt: BaseObjectReceipt,
    generation: LogicalGenerationReceipt,
    pageindex_dir: Path,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> ValidationReport:
    """Independently deep-validate one full Base and its statistics."""

    cancel = _check_cancelled(check_cancelled)
    return _capture(
        "base_invalid",
        lambda: _validate_base(
            receipt, generation, Path(pageindex_dir), cancel
        ),
    )


def validate_delta_normal(
    receipt: DeltaObjectReceipt,
    parent: SearchViewReceipt,
    target: SearchViewReceipt,
    parent_generation: LogicalGenerationReceipt,
    generation: LogicalGenerationReceipt,
    pageindex_dir: Path,
    *,
    parent_pin: ViewPin,
    target_pin: ViewPin,
    check_cancelled: Callable[[], None] | None = None,
) -> ValidationReport:
    """Validate a dirty Delta between externally trusted immutable pins."""

    cancel = _check_cancelled(check_cancelled)
    return _capture(
        "delta_invalid",
        lambda: _validate_delta(
            receipt,
            parent,
            target,
            parent_generation,
            generation,
            parent_pin,
            target_pin,
            Path(pageindex_dir),
            cancel,
        ),
    )


def validate_view_normal(
    receipt: SearchViewReceipt,
    generation: LogicalGenerationReceipt,
    pageindex_dir: Path,
    *,
    pin: ViewPin,
    check_cancelled: Callable[[], None] | None = None,
) -> ValidationReport:
    """Replay a View chain anchored by an externally trusted immutable pin."""

    cancel = _check_cancelled(check_cancelled)
    return _capture(
        "view_invalid",
        lambda: _validate_view(
            receipt, generation, pin, Path(pageindex_dir), cancel
        ),
    )


__all__ = [
    "validate_base_normal",
    "validate_delta_normal",
    "validate_generation_normal",
    "validate_view_normal",
]