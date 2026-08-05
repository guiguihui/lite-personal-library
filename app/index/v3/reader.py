"""Generation/View-pinned PageIndex v3 query reader.

The reader never consults a mutable latest/current pointer. Query work is
sparse by token, owner-filtered newest-wins, and candidate-only for chunks.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping
import copy
from dataclasses import dataclass
from pathlib import Path
import threading
from types import MappingProxyType
from typing import Any

from app.index.v2.canonical import canonical_bytes, canonical_hash
from app.index.v2.object_store import StoredSegmentRef
from app.index.v2.streaming_json import load_bounded_canonical_json

from .delta_store import DeltaObjectReceipt, load_delta_object_metadata
from .generation import LogicalGenerationReceipt
from .generation_stream import validate_generation_stream
from .layer_codec import PostingLayerReader, PostingLayerReceipt, TermRecord
from .models import (
    ChunkRef,
    GenerationRecipe,
    SearchPosting,
    SearchViewRecipe,
    TokenSummary,
    ViewPin,
    make_doc_uid,
)
from .segment_projection import ChunkMetric, SegmentProjector
from .statistics import CorpusTotals
from .view_store import (
    BaseObjectReceipt,
    SearchViewReceipt,
    ViewDocumentOwner,
    load_base_object_metadata,
    load_search_view_metadata,
    load_view_documents,
    load_view_statistics,
)


DEFAULT_CHUNK_CACHE_BYTES = 32 * 1024 * 1024


class PinnedSearchViewError(ValueError):
    """An immutable Generation/View pair cannot be opened or queried."""


@dataclass(frozen=True, slots=True)
class _LayerSession:
    layer_id: str
    receipt: PostingLayerReceipt
    reader: PostingLayerReader


@dataclass(slots=True)
class _ChunkCacheEntry:
    chunks: dict[int, dict[str, object]]
    chunk_sizes: dict[int, int]
    byte_size: int


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PinnedSearchViewError(f"{field} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise PinnedSearchViewError(f"{field} keys must be strings")
    return value


def _search_recipe(view: SearchViewReceipt) -> SearchViewRecipe:
    manifest = _mapping(
        load_bounded_canonical_json(view.root / "manifest.json"),
        "Search View manifest",
    )
    raw = _mapping(manifest.get("search_view_recipe"), "search_view_recipe")
    try:
        recipe = SearchViewRecipe(**dict(raw))
    except (TypeError, ValueError) as exc:
        raise PinnedSearchViewError(f"invalid search_view_recipe: {exc}") from exc
    if recipe.as_dict() != dict(raw):
        raise PinnedSearchViewError("search_view_recipe is not normalized")
    if canonical_hash(recipe.as_dict()) != view.search_view_recipe_hash:
        raise PinnedSearchViewError("Search View recipe hash mismatch")
    return recipe


def _load_chain(
    pageindex_dir: Path,
    view: SearchViewReceipt,
) -> tuple[BaseObjectReceipt, tuple[DeltaObjectReceipt, ...]]:
    base = load_base_object_metadata(pageindex_dir, view.base_id)
    if base.search_view_recipe_hash != view.search_view_recipe_hash:
        raise PinnedSearchViewError("View and Base recipes differ")
    expected_generation = base.generation
    expected_manifest = base.generation_manifest_sha256
    prefix: tuple[str, ...] = ()
    deltas: list[DeltaObjectReceipt] = []
    seen_views = {view.view_id}
    for delta_id in view.delta_ids:
        delta = load_delta_object_metadata(pageindex_dir, delta_id)
        parent = load_search_view_metadata(pageindex_dir, delta.parent_view_id)
        if parent.view_id in seen_views:
            raise PinnedSearchViewError("View chain contains a cycle")
        seen_views.add(parent.view_id)
        if delta.parent_view_manifest_sha256 != parent.manifest_ref.sha256:
            raise PinnedSearchViewError("Delta parent manifest binding is invalid")
        if parent.base_id != view.base_id or parent.delta_ids != prefix:
            raise PinnedSearchViewError("View Delta chain is reordered or spliced")
        if parent.search_view_recipe_hash != view.search_view_recipe_hash:
            raise PinnedSearchViewError("View chain changes SearchViewRecipe")
        if (
            parent.generation != expected_generation
            or parent.generation_manifest_sha256 != expected_manifest
        ):
            raise PinnedSearchViewError("View parent Generation boundary is invalid")
        if delta.search_view_recipe_hash != view.search_view_recipe_hash:
            raise PinnedSearchViewError("Delta changes SearchViewRecipe")
        if delta.generation == parent.generation:
            raise PinnedSearchViewError("Delta target Generation does not advance")
        deltas.append(delta)
        prefix += (delta.delta_id,)
        expected_generation = delta.generation
        expected_manifest = delta.generation_manifest_sha256
    if prefix != view.delta_ids:
        raise PinnedSearchViewError("View Delta order differs from its chain")
    if (
        view.generation != expected_generation
        or view.generation_manifest_sha256 != expected_manifest
    ):
        raise PinnedSearchViewError("View Generation differs from its Delta chain")
    return base, tuple(deltas)


def _unique_tokens(tokens: Iterable[str]) -> tuple[str, ...]:
    if isinstance(tokens, (str, bytes, bytearray)):
        raise TypeError("tokens must be an iterable of token strings")
    try:
        iterator = iter(tokens)
    except TypeError as exc:
        raise TypeError("tokens must be iterable") from exc
    unique: dict[str, None] = {}
    for token in iterator:
        if not isinstance(token, str):
            raise TypeError("tokens must contain only strings")
        unique.setdefault(token, None)
    return tuple(unique)


def _chunk_payload_size(chunk: Mapping[str, object]) -> int:
    return len(canonical_bytes(chunk))


class PinnedSearchView:
    """One immutable, externally pinned Base-plus-Delta query session."""

    def __init__(
        self,
        *,
        pageindex_dir: Path,
        pin: ViewPin,
        generation: LogicalGenerationReceipt,
        generation_recipe: GenerationRecipe,
        search_recipe: SearchViewRecipe,
        view: SearchViewReceipt,
        statistics: CorpusTotals,
        owners: dict[str, ViewDocumentOwner],
        refs_by_uid: dict[str, StoredSegmentRef],
        layers: tuple[_LayerSession, ...],
        active_ordinals_by_layer: dict[str, frozenset[int]],
        chunk_cache_bytes: int,
    ) -> None:
        self.pageindex_dir = pageindex_dir
        self.pin = pin
        self.generation = generation
        self.generation_recipe = generation_recipe
        self.search_recipe = search_recipe
        self.view = view
        self._statistics = statistics
        self._owners = owners
        self._owner_view = MappingProxyType(owners)
        self._refs_by_uid = refs_by_uid
        self._layers_chronological = layers
        self._layers_newest = tuple(reversed(layers))
        self._layers_by_id = {layer.layer_id: layer for layer in layers}
        self._active_ordinals_by_layer = MappingProxyType(
            active_ordinals_by_layer
        )
        self._projector = SegmentProjector(pageindex_dir)
        self._chunk_cache_limit = chunk_cache_bytes
        self._chunk_cache_size = 0
        self._chunk_cache: OrderedDict[str, _ChunkCacheEntry] = OrderedDict()
        self._cache_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._closed = False

    @classmethod
    def open(
        cls,
        pageindex_dir: Path,
        pin: ViewPin,
        generation: LogicalGenerationReceipt,
        *,
        read_observer: Callable[[str, str, int, int], None] | None = None,
        chunk_cache_bytes: int = DEFAULT_CHUNK_CACHE_BYTES,
    ) -> "PinnedSearchView":
        """Open exactly the supplied immutable pin; never resolve latest state."""

        if not isinstance(pin, ViewPin):
            raise TypeError("pin must be a ViewPin")
        if not isinstance(generation, LogicalGenerationReceipt):
            raise TypeError("generation must be a LogicalGenerationReceipt")
        if read_observer is not None and not callable(read_observer):
            raise TypeError("read_observer must be callable")
        if (
            isinstance(chunk_cache_bytes, bool)
            or not isinstance(chunk_cache_bytes, int)
            or chunk_cache_bytes < 0
        ):
            raise ValueError("chunk_cache_bytes must be an integer >= 0")
        root = Path(pageindex_dir).absolute()
        if pin.generation != generation.generation_id:
            raise PinnedSearchViewError("pin and Generation receipt differ")
        view = load_search_view_metadata(root, pin.view_id)
        if view.view_id != pin.view_id or view.generation != pin.generation:
            raise PinnedSearchViewError("Search View differs from the exact pin")
        if view.generation_manifest_sha256 != generation.manifest_ref.sha256:
            raise PinnedSearchViewError("View and Generation manifest binding differ")

        recipes: list[GenerationRecipe] = []
        refs_by_doc_key = validate_generation_stream(
            generation,
            root,
            check_cancelled=lambda: None,
            collect_refs=True,
            recipe_observer=recipes.append,
        )
        if len(recipes) != 1:
            raise PinnedSearchViewError("Generation recipe was not validated exactly once")
        generation_recipe = recipes[0]
        base, deltas = _load_chain(root, view)
        search_recipe = _search_recipe(view)
        if base.search_view_recipe_hash != canonical_hash(search_recipe.as_dict()):
            raise PinnedSearchViewError("Base and View recipes differ")
        for delta in deltas:
            if delta.search_view_recipe_hash != view.search_view_recipe_hash:
                raise PinnedSearchViewError("Delta and View recipes differ")

        statistics = load_view_statistics(view)
        owners = load_view_documents(view)
        if statistics.documents != len(owners):
            raise PinnedSearchViewError("View owner count differs from statistics")
        if len(owners) != len(refs_by_doc_key):
            raise PinnedSearchViewError("View owner count differs from Generation")

        sessions: list[_LayerSession] = []
        try:
            layer_inputs = (
                (base.base_id, base.layer),
                *((delta.delta_id, delta.layer) for delta in deltas),
            )
            for layer_id, receipt in layer_inputs:
                observer = None
                if read_observer is not None:
                    def observer(
                        name: str,
                        offset: int,
                        size: int,
                        *,
                        _layer_id: str = layer_id,
                    ) -> None:
                        read_observer(_layer_id, name, offset, size)
                reader = PostingLayerReader(
                    receipt,
                    recipe=search_recipe,
                    read_observer=observer,
                )
                sessions.append(_LayerSession(layer_id, receipt, reader))

            layers_by_id = {layer.layer_id: layer for layer in sessions}
            active_ordinals_by_layer: dict[str, set[int]] = {
                layer.layer_id: set() for layer in sessions
            }
            refs_by_uid: dict[str, StoredSegmentRef] = {}
            for doc_uid, owner in owners.items():
                if doc_uid != make_doc_uid(owner.doc_key):
                    raise PinnedSearchViewError("owner doc_uid differs from doc_key")
                ref = refs_by_doc_key.pop(owner.doc_key, None)
                if ref is None or ref.segment_hash != owner.segment_hash:
                    raise PinnedSearchViewError("owner differs from Generation")
                layer = layers_by_id.get(owner.owner_layer_id)
                if layer is None:
                    raise PinnedSearchViewError("owner references an unopened layer")
                routed = layer.reader._documents_by_uid.get(doc_uid)
                if routed is None:
                    raise PinnedSearchViewError("owner document is absent from its layer")
                ordinal, document = routed
                if (
                    ordinal != owner.doc_ordinal
                    or document.doc_key != owner.doc_key
                    or document.segment_hash != owner.segment_hash
                ):
                    raise PinnedSearchViewError("owner route differs from its layer")
                active_ordinals_by_layer[owner.owner_layer_id].add(ordinal)
                refs_by_uid[doc_uid] = ref
            if refs_by_doc_key:
                raise PinnedSearchViewError("View omits Generation documents")
            return cls(
                pageindex_dir=root,
                pin=pin,
                generation=generation,
                generation_recipe=generation_recipe,
                search_recipe=search_recipe,
                view=view,
                statistics=statistics,
                owners=owners,
                refs_by_uid=refs_by_uid,
                layers=tuple(sessions),
                active_ordinals_by_layer={
                    layer_id: frozenset(ordinals)
                    for layer_id, ordinals in active_ordinals_by_layer.items()
                },
                chunk_cache_bytes=chunk_cache_bytes,
            )
        except BaseException:
            for session in reversed(sessions):
                try:
                    session.reader.close()
                except BaseException:
                    # Opening failed before ownership transferred to the
                    # returned PinnedSearchView. Cleanup is best-effort: a
                    # secondary close failure must neither mask the primary
                    # authentication/open error nor stop remaining cleanup.
                    pass
            raise

    def __enter__(self) -> "PinnedSearchView":
        self._ensure_open()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> bool:
        self.close()
        return False

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("PinnedSearchView is closed")

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            first: BaseException | None = None
            for layer in reversed(self._layers_chronological):
                try:
                    layer.reader.close()
                except BaseException as exc:
                    if first is None:
                        first = exc
            with self._cache_lock:
                self._chunk_cache.clear()
                self._chunk_cache_size = 0
            # A closed reader has no usable query surface. Detach the O(N)
            # owner/ref/layer state so a long-lived caller retaining this
            # session does not retain the complete pinned view in memory.
            self._owners = {}
            self._owner_view = MappingProxyType(self._owners)
            self._refs_by_uid = {}
            self._layers_chronological = ()
            self._layers_newest = ()
            self._layers_by_id = {}
            self._active_ordinals_by_layer = MappingProxyType({})
            if first is not None:
                raise first

    def corpus_stats(self) -> CorpusTotals:
        with self._state_lock:
            self._ensure_open()
            return self._statistics

    def documents(self) -> Mapping[str, ViewDocumentOwner]:
        with self._state_lock:
            self._ensure_open()
            return self._owner_view

    def document_chunk_refs(self, doc_uids: Iterable[str]) -> tuple[ChunkRef, ...]:
        """Return every active chunk reference for the selected documents.

        Document order follows the input and local IDs are emitted in ascending
        order. The chunk counts come from the authenticated owner layer's
        document table, so enumerating candidates never reads Segment payloads
        or creates a View-wide chunk catalog.
        """

        if isinstance(doc_uids, (str, bytes, bytearray)):
            raise TypeError("doc_uids must be an iterable of document UID strings")
        try:
            iterator = iter(doc_uids)
        except TypeError as exc:
            raise TypeError("doc_uids must be iterable") from exc
        with self._state_lock:
            self._ensure_open()
            refs: list[ChunkRef] = []
            seen: set[str] = set()
            for doc_uid in iterator:
                if not isinstance(doc_uid, str):
                    raise TypeError("doc_uids must contain only strings")
                if doc_uid in seen:
                    raise ValueError("duplicate document UID request")
                owner = self._owners.get(doc_uid)
                if owner is None:
                    raise PinnedSearchViewError(
                        "document UID is not active in the pinned View"
                    )
                layer = self._layers_by_id[owner.owner_layer_id]
                routed = layer.reader._documents_by_uid.get(doc_uid)
                if routed is None or routed[0] != owner.doc_ordinal:
                    raise PinnedSearchViewError("document owner route is invalid")
                document = routed[1]
                if (
                    document.doc_key != owner.doc_key
                    or document.segment_hash != owner.segment_hash
                ):
                    raise PinnedSearchViewError("document owner route is invalid")
                refs.extend(
                    ChunkRef(doc_uid, owner.segment_hash, local_id)
                    for local_id in range(document.chunk_count)
                )
                seen.add(doc_uid)
            return tuple(refs)

    def _summary(
        self, token: str, values: tuple[int, int, int]
    ) -> TokenSummary | None:
        df_any, df_nonbody, df_body = values
        if values == (0, 0, 0):
            return None
        if (
            min(values) < 0
            or max(values) > self._statistics.total_chunks
            or max(df_nonbody, df_body) > df_any
            or df_any > df_nonbody + df_body
            or df_any == 0
        ):
            raise PinnedSearchViewError(f"invalid pinned token statistics: {token!r}")
        return TokenSummary(token, df_any, df_nonbody, df_body)

    def token_stats(
        self, tokens: Iterable[str]
    ) -> dict[str, TokenSummary | None]:
        requested = _unique_tokens(tokens)
        with self._state_lock:
            self._ensure_open()
            totals = {token: [0, 0, 0] for token in requested}
            for layer in self._layers_newest:
                records = layer.reader.lookup_terms(requested)
                for token, record in records.items():
                    if record is None:
                        continue
                    values = totals[token]
                    for position, value in enumerate(record.delta):
                        values[position] += value
            return {
                token: self._summary(token, tuple(totals[token]))
                for token in requested
            }

    def _resolve_token(
        self, token: str
    ) -> tuple[
        TokenSummary | None,
        tuple[tuple[_LayerSession, TermRecord | None], ...],
    ]:
        records: list[tuple[_LayerSession, TermRecord | None]] = []
        totals = [0, 0, 0]
        for layer in self._layers_newest:
            record = layer.reader.lookup_term(token)
            records.append((layer, record))
            if record is not None:
                for position, value in enumerate(record.delta):
                    totals[position] += value
        return self._summary(token, tuple(totals)), tuple(records)


    def _iter_postings(self, token: str, *, effective: bool) -> Iterator[SearchPosting]:
        if not isinstance(token, str):
            raise TypeError("token must be a string")
        with self._state_lock:
            self._ensure_open()
            summary, records = self._resolve_token(token)
            if summary is None:
                return iter(())
            prune_body = effective and (
                summary.df_body >= self.generation_recipe.body_df_min
                and summary.df_body
                * self.generation_recipe.body_df_ratio_denominator
                >= self._statistics.total_chunks
                * self.generation_recipe.body_df_ratio_numerator
            )
            rows: list[SearchPosting] = []
            for layer, record in records:
                if record is None or not record.has_postings:
                    continue
                active_ordinals = self._active_ordinals_by_layer[layer.layer_id]
                source = (
                    layer.reader._iter_nonbody_only(
                        record,
                        active_document_predicate=active_ordinals.__contains__,
                    )
                    if prune_body
                    else layer.reader._iter_complete(
                        record,
                        active_document_predicate=active_ordinals.__contains__,
                    )
                )
                rows.extend(source)
            rows.sort(
                key=lambda row: (
                    row.chunk_ref.doc_uid,
                    row.chunk_ref.segment_hash,
                    row.chunk_ref.local_id,
                )
            )
            return iter(rows)

    def iter_raw_postings(self, token: str) -> Iterator[SearchPosting]:
        return self._iter_postings(token, effective=False)

    def iter_effective_postings(self, token: str) -> Iterator[SearchPosting]:
        return self._iter_postings(token, effective=True)

    def _requested_refs(self, refs: Iterable[ChunkRef]) -> tuple[ChunkRef, ...]:
        if isinstance(refs, (str, bytes, bytearray)):
            raise TypeError("refs must be an iterable of ChunkRef values")
        try:
            iterator = iter(refs)
        except TypeError as exc:
            raise TypeError("refs must be iterable") from exc
        requested: list[ChunkRef] = []
        seen: set[ChunkRef] = set()
        for ref in iterator:
            if not isinstance(ref, ChunkRef):
                raise TypeError("refs must contain only ChunkRef values")
            if ref in seen:
                raise ValueError("duplicate ChunkRef request")
            owner = self._owners.get(ref.doc_uid)
            if owner is None or owner.segment_hash != ref.segment_hash:
                raise PinnedSearchViewError("ChunkRef is not active in the pinned View")
            layer = self._layers_by_id[owner.owner_layer_id]
            routed = layer.reader._documents_by_uid.get(ref.doc_uid)
            if routed is None or routed[0] != owner.doc_ordinal:
                raise PinnedSearchViewError("ChunkRef owner route is invalid")
            if ref.local_id >= routed[1].chunk_count:
                raise PinnedSearchViewError("ChunkRef local_id is outside the document")
            requested.append(ref)
            seen.add(ref)
        return tuple(requested)

    def get_chunk_metrics(
        self, refs: Iterable[ChunkRef]
    ) -> dict[ChunkRef, ChunkMetric]:
        with self._state_lock:
            self._ensure_open()
            requested = self._requested_refs(refs)
            grouped: dict[str, list[ChunkRef]] = {}
            for ref in requested:
                owner = self._owners[ref.doc_uid]
                grouped.setdefault(owner.owner_layer_id, []).append(ref)
            observed: dict[ChunkRef, ChunkMetric] = {}
            for layer_id, grouped_refs in grouped.items():
                observed.update(
                    self._layers_by_id[layer_id].reader.get_chunk_metrics(grouped_refs)
                )
            return {ref: observed[ref] for ref in requested}

    def _load_cached_chunks(
        self, ref: StoredSegmentRef, local_ids: tuple[int, ...]
    ) -> dict[int, dict[str, object]]:
        with self._cache_lock:
            entry = self._chunk_cache.get(ref.segment_hash)
            if entry is not None and all(
                local_id in entry.chunks for local_id in local_ids
            ):
                self._chunk_cache.move_to_end(ref.segment_hash)
                return {
                    local_id: copy.deepcopy(entry.chunks[local_id])
                    for local_id in local_ids
                }
            cached = {} if entry is None else dict(entry.chunks)
            cached_sizes = {} if entry is None else dict(entry.chunk_sizes)
        missing = tuple(local_id for local_id in local_ids if local_id not in cached)
        loaded = self._projector.load_chunks(ref, missing) if missing else {}
        loaded_sizes = {
            local_id: _chunk_payload_size(chunk)
            for local_id, chunk in loaded.items()
        }
        with self._cache_lock:
            current = self._chunk_cache.pop(ref.segment_hash, None)
            if current is not None:
                self._chunk_cache_size -= current.byte_size
                cached.update(current.chunks)
                cached_sizes.update(current.chunk_sizes)
            cached.update(loaded)
            cached_sizes.update(loaded_sizes)
            byte_size = sum(cached_sizes.values())
            if self._chunk_cache_limit and byte_size <= self._chunk_cache_limit:
                self._chunk_cache[ref.segment_hash] = _ChunkCacheEntry(
                    cached, cached_sizes, byte_size
                )
                self._chunk_cache_size += byte_size
                while (
                    self._chunk_cache
                    and self._chunk_cache_size > self._chunk_cache_limit
                ):
                    _key, evicted = self._chunk_cache.popitem(last=False)
                    self._chunk_cache_size -= evicted.byte_size
            return {
                local_id: copy.deepcopy(cached[local_id])
                for local_id in local_ids
            }

    def get_chunks(
        self, refs: Iterable[ChunkRef]
    ) -> dict[ChunkRef, dict[str, object]]:
        with self._state_lock:
            self._ensure_open()
            requested = self._requested_refs(refs)
            grouped: dict[tuple[str, str], list[ChunkRef]] = {}
            for ref in requested:
                grouped.setdefault((ref.doc_uid, ref.segment_hash), []).append(ref)
            by_ref: dict[ChunkRef, dict[str, object]] = {}
            for grouped_refs in grouped.values():
                first = grouped_refs[0]
                stored = self._refs_by_uid[first.doc_uid]
                local_ids = tuple(sorted(ref.local_id for ref in grouped_refs))
                chunks = self._load_cached_chunks(stored, local_ids)
                for chunk_ref in grouped_refs:
                    by_ref[chunk_ref] = chunks[chunk_ref.local_id]
            return {ref: by_ref[ref] for ref in requested}


__all__ = [
    "DEFAULT_CHUNK_CACHE_BYTES",
    "PinnedSearchView",
    "PinnedSearchViewError",
]
