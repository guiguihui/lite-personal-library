"""One-Segment-at-a-time construction of immutable PageIndex v3 base Views."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any, BinaryIO

from app.index.v2.canonical import iter_canonical_json
from app.index.v2.object_store import StoredSegmentRef

from .generation import (
    LogicalGenerationReceipt,
    validate_logical_generation_inputs,
)
from .layer_runs import StagedLayerBuilder
from .models import (
    MAX_U64,
    GenerationRecipe,
    SearchViewRecipe,
    make_doc_uid,
)
from .segment_projection import SegmentProjector
from .statistics import CorpusTotals
from .summary_store import put_summary
from .view_store import (
    BaseObjectReceipt,
    SearchViewReceipt,
    ViewDocumentOwner,
    ViewStoreConflictError,
    finalize_base_object,
    finalize_search_view,
    write_base_candidate,
    write_search_view_candidate,
)


_OWNER_KEYS = {
    "doc_key",
    "doc_ordinal",
    "doc_uid",
    "segment_hash",
    "summary_bytes",
    "summary_sha256",
}


def _checked_add(name: str, left: int, right: int) -> int:
    value = left + right
    if value > MAX_U64:
        raise OverflowError(f"{name} exceeds u64")
    return value


def _write_owner_record(stream: BinaryIO, value: Mapping[str, object]) -> None:
    for fragment in iter_canonical_json(value):
        stream.write(fragment.encode("utf-8"))
    stream.write(b"\n")


def _strict_owner_record(raw: bytes, position: int) -> Mapping[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid staged owner record {position}") from exc
    if not isinstance(value, Mapping) or set(value) != _OWNER_KEYS:
        raise ValueError(f"invalid staged owner record shape at {position}")
    return value


def _iter_owners(
    path: Path,
    *,
    base_id: str,
    expected_records: int,
    check_cancelled: Callable[[], None],
) -> Iterator[tuple[str, ViewDocumentOwner]]:
    observed = 0
    previous: bytes | None = None
    with path.open("rb") as stream:
        for raw in stream:
            check_cancelled()
            if not raw.endswith(b"\n"):
                raise ValueError("staged owner spool ends inside a record")
            value = _strict_owner_record(raw[:-1], observed)
            doc_uid = value["doc_uid"]
            if not isinstance(doc_uid, str):
                raise ValueError("staged owner doc_uid must be a string")
            encoded = doc_uid.encode("utf-8")
            if previous is not None and encoded <= previous:
                raise ValueError("staged owners are not strictly sorted by doc_uid")
            previous = encoded
            try:
                owner = ViewDocumentOwner(
                    doc_key=value["doc_key"],
                    segment_hash=value["segment_hash"],
                    summary_sha256=value["summary_sha256"],
                    summary_bytes=value["summary_bytes"],
                    owner_layer_kind="base",
                    owner_layer_id=base_id,
                    doc_ordinal=value["doc_ordinal"],
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"invalid staged owner record {observed}: {exc}"
                ) from exc
            observed += 1
            if observed > expected_records:
                raise ValueError("staged owner spool contains too many records")
            yield doc_uid, owner
    if observed != expected_records:
        raise ValueError(
            "staged owner spool record count does not match corpus documents"
        )


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
            add_note(f"cleaning base build scratch also failed: {exc}")


def build_base_view(
    pageindex_dir: Path,
    refs: Iterable[StoredSegmentRef],
    generation: LogicalGenerationReceipt,
    generation_recipe: GenerationRecipe,
    search_view_recipe: SearchViewRecipe | None = None,
    *,
    max_run_bytes: int = 64 * 1024 * 1024,
    merge_fan_in: int = 32,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[BaseObjectReceipt, SearchViewReceipt]:
    """Build and publish a deterministic full base and its zero-delta View.

    Only lightweight document references and a disk owner spool span documents.
    Each decoded Segment, summary, and chunk-metric container is released before
    the next Segment is loaded.
    """

    root = Path(pageindex_dir)
    if not isinstance(generation, LogicalGenerationReceipt):
        raise TypeError("generation must be a LogicalGenerationReceipt")
    if not isinstance(generation_recipe, GenerationRecipe):
        raise TypeError("generation_recipe must be a GenerationRecipe")
    physical_recipe = (
        SearchViewRecipe() if search_view_recipe is None else search_view_recipe
    )
    if not isinstance(physical_recipe, SearchViewRecipe):
        raise TypeError("search_view_recipe must be a SearchViewRecipe")
    if check_cancelled is None:
        cancel = lambda: None
    elif callable(check_cancelled):
        cancel = check_cancelled
    else:
        raise TypeError("check_cancelled must be callable")

    cancel()
    validated_refs = validate_logical_generation_inputs(
        refs,
        generation,
        generation_recipe,
        check_cancelled=cancel,
    )
    layer_refs = list(validated_refs)
    del validated_refs
    layer_refs.sort(
        key=lambda ref: make_doc_uid(ref.doc_key).encode("utf-8")
    )
    cancel()

    root.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(dir=root, prefix=".piv3-base-build."))
    base_candidate_dir = scratch / "base"
    view_candidate_dir = scratch / "view"
    owners_path = scratch / "owners.jsonl"
    primary: BaseException | None = None
    retain_scratch = False
    try:
        projector = SegmentProjector(root)
        scalar = {
            "documents": 0,
            "total_chunks": 0,
            "title_length_sum": 0,
            "breadcrumb_length_sum": 0,
            "body_length_sum": 0,
            "posting_count": 0,
        }

        with StagedLayerBuilder(
            base_candidate_dir,
            layer_kind="base",
            recipe=physical_recipe,
            max_run_bytes=max_run_bytes,
            merge_fan_in=merge_fan_in,
            check_cancelled=cancel,
        ) as stage:
            with owners_path.open("wb", buffering=256 * 1024) as owner_spool:
                for ref in layer_refs:
                    cancel()
                    doc_uid = make_doc_uid(ref.doc_key)
                    ticket = stage.begin_document(
                        ref.doc_key,
                        doc_uid,
                        ref.segment_hash,
                    )
                    summary, metrics = projector.project_to_sink(
                        ref,
                        ticket.add_posting,
                    )
                    cancel()
                    summary_ref = put_summary(root, summary)
                    cancel()
                    ordinal = ticket.ordinal
                    ticket.commit(summary.chunk_count, iter(metrics))

                    scalar["documents"] = _checked_add(
                        "documents", scalar["documents"], 1
                    )
                    scalar["total_chunks"] = _checked_add(
                        "total_chunks",
                        scalar["total_chunks"],
                        summary.chunk_count,
                    )
                    scalar["title_length_sum"] = _checked_add(
                        "title_length_sum",
                        scalar["title_length_sum"],
                        summary.title_length_sum,
                    )
                    scalar["breadcrumb_length_sum"] = _checked_add(
                        "breadcrumb_length_sum",
                        scalar["breadcrumb_length_sum"],
                        summary.breadcrumb_length_sum,
                    )
                    scalar["body_length_sum"] = _checked_add(
                        "body_length_sum",
                        scalar["body_length_sum"],
                        summary.body_length_sum,
                    )
                    scalar["posting_count"] = _checked_add(
                        "posting_count",
                        scalar["posting_count"],
                        summary.posting_count,
                    )
                    _write_owner_record(
                        owner_spool,
                        {
                            "doc_key": summary.doc_key,
                            "doc_ordinal": ordinal,
                            "doc_uid": summary.doc_uid,
                            "segment_hash": summary.segment_hash,
                            "summary_bytes": summary_ref.byte_size,
                            "summary_sha256": summary_ref.summary_sha256,
                        },
                    )
                    del ticket, summary_ref, summary, metrics
                    cancel()
                layer = stage.finish()
        del layer_refs

        if layer.document_count != scalar["documents"]:
            raise ValueError("base layer document count differs from summaries")
        if layer.chunk_count != scalar["total_chunks"]:
            raise ValueError("base layer chunk count differs from summaries")
        statistics = CorpusTotals(
            documents=scalar["documents"],
            total_chunks=scalar["total_chunks"],
            token_count=layer.term_count,
            title_length_sum=scalar["title_length_sum"],
            breadcrumb_length_sum=scalar["breadcrumb_length_sum"],
            body_length_sum=scalar["body_length_sum"],
            posting_count=scalar["posting_count"],
        )
        cancel()

        base_candidate = write_base_candidate(
            base_candidate_dir,
            generation=generation,
            recipe=physical_recipe,
            layer=layer,
            statistics=statistics,
        )
        base = finalize_base_object(root, base_candidate)
        cancel()

        view_candidate = write_search_view_candidate(
            view_candidate_dir,
            generation=generation,
            recipe=physical_recipe,
            base=base,
            statistics=statistics,
            documents=_iter_owners(
                owners_path,
                base_id=base.base_id,
                expected_records=statistics.documents,
                check_cancelled=cancel,
            ),
            delta_ids=(),
        )
        view = finalize_search_view(root, view_candidate)
        return base, view
    except BaseException as exc:
        primary = exc
        retain_scratch = isinstance(exc, ViewStoreConflictError)
        raise
    finally:
        if not retain_scratch:
            _remove_scratch(scratch, primary)


__all__ = ["build_base_view"]
