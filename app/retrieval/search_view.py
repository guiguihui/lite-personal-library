"""Candidate-only retrieval over an immutable PageIndex v3 Search View.

This module preserves the legacy B (BM25F), A (title/breadcrumb phrase), and
E (document routing) paths without materializing a corpus-wide chunk table or
CID map.  Query work is limited to sparse token postings and addressed chunks.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import math
import re
from typing import Any

from app.index.v3.models import ChunkRef, SearchPosting, TokenSummary
from app.index.v3.reader import PinnedSearchView, PinnedSearchViewError
from app.index.v3.segment_projection import ChunkMetric
from app.index.v3.view_store import ViewDocumentOwner
from app.retrieval.bm25 import BM25_B, BM25_K, CHUNK_FIELD_BOOST
from app.retrieval.fuse import rrf_fuse
from app.retrieval.search import Hit, _js_round, _per_doc_truncate
from app.text.normalization import normalize_for_search
from app.retrieval.tokenizer import expand_query_weighted, tokenize, tokenize_unique

__all__ = ["search_pinned_view"]


_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_EN_LOWER_RE = re.compile(r"[a-z][a-z0-9]{1,}")
_FIELDS = ("title", "breadcrumb", "body")


def _body_is_pruned(
    view: PinnedSearchView,
    summary: TokenSummary | None,
    total_chunks: int,
) -> bool:
    if summary is None:
        return False
    recipe = view.generation_recipe
    return (
        summary.df_body >= recipe.body_df_min
        and summary.df_body * recipe.body_df_ratio_denominator
        >= total_chunks * recipe.body_df_ratio_numerator
    )


def _effective_df(
    view: PinnedSearchView,
    summary: TokenSummary | None,
    total_chunks: int,
) -> int:
    if summary is None:
        return 0
    if _body_is_pruned(view, summary, total_chunks):
        return summary.df_nonbody
    return summary.df_any


def _ordered_refs(
    refs: Iterable[ChunkRef],
    owners: Mapping[str, ViewDocumentOwner],
) -> tuple[ChunkRef, ...]:
    """Return legacy compiler order: document key, then Segment local ID."""

    return tuple(
        sorted(
            refs,
            key=lambda ref: (owners[ref.doc_uid].doc_key, ref.local_id),
        )
    )


def _breadcrumb_text(chunk: Mapping[str, Any]) -> str:
    breadcrumb = chunk.get("breadcrumb") or []
    return (
        " ".join(str(part) for part in breadcrumb)
        if isinstance(breadcrumb, list)
        else str(breadcrumb)
    )


def _legacy_node_id(chunk: Mapping[str, Any]) -> str:
    """Use the raw compatibility ID that defines legacy Hit/RRF identity."""

    value = chunk.get("legacy_node_id") or chunk.get("node_id")
    if isinstance(value, str) and value:
        return value
    # Segment v2 production chunks carry ``legacy_node_id``.  ``node_key`` is
    # only a compatibility fallback for hand-built/older test Segments; it is
    # deterministic but intentionally does not replace the legacy ID when one
    # exists.
    node_key = chunk.get("node_key")
    return str(node_key or "")


def _decorate_chunk(
    raw_chunk: Mapping[str, Any],
    ref: ChunkRef,
    owner: ViewDocumentOwner,
    view: PinnedSearchView,
) -> dict[str, Any]:
    doc_type, slug = owner.doc_key.split(":", 1)
    node_id = _legacy_node_id(raw_chunk)
    result = dict(raw_chunk)
    # Additive compatibility and immutable-reference fields.  Existing raw
    # Segment fields remain untouched.
    result.update(
        {
            "doc_id": slug,
            "node_id": node_id,
            "doc_type": doc_type,
            "slug": slug,
            "generation": view.pin.generation,
            "view_id": view.pin.view_id,
            "doc_key": owner.doc_key,
            "doc_uid": ref.doc_uid,
            "segment_hash": ref.segment_hash,
            "local_id": ref.local_id,
            "node_key": str(raw_chunk.get("node_key") or ""),
        }
    )
    return result


def _node_from_chunk(chunk: Mapping[str, Any]) -> dict[str, Any]:
    body = str(chunk.get("body") or "")
    return {
        "doc_id": chunk.get("doc_id"),
        "node_id": chunk.get("node_id"),
        "title": chunk.get("title"),
        "breadcrumb": chunk.get("breadcrumb"),
        "url": "",
        "terms": [],
        "summary": body[:200],
        "line_num": chunk.get("line_num"),
        "doc_type": chunk.get("doc_type"),
        "slug": chunk.get("slug"),
        "generation": chunk.get("generation"),
        "view_id": chunk.get("view_id"),
        "doc_key": chunk.get("doc_key"),
        "doc_uid": chunk.get("doc_uid"),
        "segment_hash": chunk.get("segment_hash"),
        "local_id": chunk.get("local_id"),
        "node_key": chunk.get("node_key"),
    }


def _hit_from_chunk(
    chunk: Mapping[str, Any],
    *,
    score: float,
    tokens: list[str],
    positions: dict[str, dict[str, int]],
) -> Hit:
    """Build a Hit with both legacy payload and stable reference metadata."""

    return Hit(
        node=_node_from_chunk(chunk),
        score=score,
        tokens=tokens,
        positions=positions,
        chunk=chunk,
        generation=str(chunk.get("generation") or "") or None,
        view_id=str(chunk.get("view_id") or "") or None,
        doc_key=str(chunk.get("doc_key") or "") or None,
        doc_uid=str(chunk.get("doc_uid") or "") or None,
        segment_hash=str(chunk.get("segment_hash") or "") or None,
        local_id=(
            chunk.get("local_id")
            if isinstance(chunk.get("local_id"), int)
            else None
        ),
        node_key=str(chunk.get("node_key") or "") or None,
    )


def _positions(
    tokens: list[str],
    chunk: Mapping[str, Any],
    postings: Mapping[str, SearchPosting],
) -> dict[str, dict[str, int]]:
    """Build legacy positions, restricted to fields retained by policy."""

    field_tokens = {
        "title": tokenize(str(chunk.get("title") or "")),
        "breadcrumb": tokenize(_breadcrumb_text(chunk)),
        "body": tokenize(str(chunk.get("body") or "")),
    }
    positions: dict[str, dict[str, int]] = {}
    for token in tokens:
        posting = postings.get(token)
        if posting is None:
            continue
        field_tfs = (
            posting.title_tf,
            posting.breadcrumb_tf,
            posting.body_tf,
        )
        observed: dict[str, int] = {}
        for field, tf in zip(_FIELDS, field_tfs):
            if tf <= 0:
                continue
            try:
                position = field_tokens[field].index(token)
            except ValueError:
                # The immutable posting/Segment validator makes this
                # unreachable for a real PinnedSearchView.
                continue
            observed["summary" if field == "body" else field] = position
        if observed:
            positions[token] = observed
    return positions


def _bm25_score(
    tokens: list[str],
    weights: Mapping[str, float],
    postings: Mapping[str, SearchPosting],
    metric: ChunkMetric,
    *,
    effective_df: Mapping[str, int],
    total_chunks: int,
    field_averages: Mapping[str, float],
    average_length: float,
) -> float:
    lengths = {
        "title": metric.title_length,
        "breadcrumb": metric.breadcrumb_length,
        "body": metric.body_length,
    }
    total = 0.0
    for field in _FIELDS:
        doc_len = lengths[field]
        avg_len = field_averages.get(field) or average_length or 1
        for token in tokens:
            posting = postings.get(token)
            if posting is None:
                continue
            tf = (
                posting.title_tf
                if field == "title"
                else posting.breadcrumb_tf
                if field == "breadcrumb"
                else posting.body_tf
            )
            if not tf:
                continue
            df = effective_df.get(token, 0)
            idf = math.log(
                1 + (total_chunks - df + 0.5) / (df + 0.5)
            )
            norm = 1 - BM25_B + BM25_B * (doc_len / avg_len)
            total += (
                idf
                * ((tf * (BM25_K + 1)) / (tf + BM25_K * norm))
                * CHUNK_FIELD_BOOST[field]
                * (weights.get(token) or 0)
            )
    return total


def _phrase_path(query: str, candidates: list[Hit], top_k: int = 20) -> list[Hit]:
    raw = (query or "").lower()
    cjk_part = "".join(_CJK_RE.findall(raw))
    en_tokens = _EN_LOWER_RE.findall(raw)
    out: list[Hit] = []
    for hit in candidates:
        chunk = hit.chunk
        if chunk is None:
            continue
        title_text = (
            str(chunk.get("title") or "") + " " + _breadcrumb_text(chunk)
        ).lower()
        phrase_hits = 0
        phrase_total = 0
        for index in range(len(cjk_part) - 1):
            phrase_total += 1
            if cjk_part[index : index + 2] in title_text:
                phrase_hits += 1
        for index in range(len(en_tokens) - 1):
            phrase_total += 1
            if f"{en_tokens[index]} {en_tokens[index + 1]}" in title_text:
                phrase_hits += 1
        for token in en_tokens:
            phrase_total += 1
            if token in title_text:
                phrase_hits += 1
        phrase_score = phrase_hits / phrase_total if phrase_total else 0
        if phrase_score > 0.3:
            out.append(
                _hit_from_chunk(
                    chunk,
                    score=_js_round(phrase_score, 2),
                    tokens=hit.tokens,
                    positions=hit.positions,
                )
            )
    out.sort(key=lambda hit: hit.score, reverse=True)
    return out[:top_k]


def _route_documents(
    tokens: list[str],
    weights: Mapping[str, float],
    postings_by_ref: Mapping[ChunkRef, Mapping[str, SearchPosting]],
    owners: Mapping[str, ViewDocumentOwner],
) -> tuple[str, ...]:
    """Choose at most five documents from non-body query evidence.

    P3 deliberately has no eager global document-title table.  Title and
    breadcrumb postings are the authenticated sparse equivalent used here;
    body-only evidence can never route an entire document.
    """

    matched: dict[str, dict[str, tuple[bool, bool]]] = {}
    for ref, by_token in postings_by_ref.items():
        doc = matched.setdefault(ref.doc_uid, {})
        for token in tokens:
            posting = by_token.get(token)
            if posting is None or not (posting.title_tf or posting.breadcrumb_tf):
                continue
            prior = doc.get(token, (False, False))
            doc[token] = (
                prior[0] or posting.title_tf > 0,
                prior[1] or posting.breadcrumb_tf > 0,
            )

    scored: list[tuple[str, float, str]] = []
    for doc_uid, evidence in matched.items():
        score = 0.0
        for token, (in_title, in_breadcrumb) in evidence.items():
            weight = weights.get(token) or 0
            score += weight * ((2 if in_title else 0) + (1 if in_breadcrumb else 0))
        if score > 0:
            scored.append((doc_uid, score, owners[doc_uid].doc_key))
    scored.sort(key=lambda item: (-item[1], item[2]))
    return tuple(doc_uid for doc_uid, _score, _doc_key in scored[:5])


def _document_refs(
    view: PinnedSearchView,
    doc_uids: tuple[str, ...],
) -> tuple[ChunkRef, ...]:
    if not doc_uids:
        return ()
    refs = view.document_chunk_refs(doc_uids)
    if isinstance(refs, Mapping):
        flattened: list[ChunkRef] = []
        for doc_uid in doc_uids:
            flattened.extend(refs.get(doc_uid, ()))
        return tuple(flattened)
    return tuple(refs)


def _truncate_scored_refs(
    scored: list[tuple[ChunkRef, float]],
    owners: Mapping[str, ViewDocumentOwner],
    top_k: int,
) -> tuple[ChunkRef, ...]:
    """Apply the Hit per-document cap before any chunk payload is loaded."""

    counts: dict[str, int] = {}
    result: list[ChunkRef] = []
    for ref, _score in scored:
        doc_key = owners[ref.doc_uid].doc_key
        count = counts.get(doc_key, 0)
        if count < 3:
            result.append(ref)
            counts[doc_key] = count + 1
        if len(result) >= top_k * 2:
            break
    return tuple(result[:top_k])


def search_pinned_view(
    query: str,
    view: PinnedSearchView,
    top_k: int = 50,
) -> list[Hit]:
    """Run legacy-compatible multi-path search against one immutable pin.

    Only query-token posting partitions, candidate PCV metrics, and candidate
    Segment chunks are read.  The function never resolves mutable current
    state and never calls the legacy global chunk-statistics builder.
    """

    if not isinstance(query, str):
        raise TypeError("query must be a string")
    if isinstance(top_k, bool) or not isinstance(top_k, int):
        raise TypeError("top_k must be an integer")
    if top_k <= 0:
        return []

    normalized_query = normalize_for_search(query)
    original_tokens = tokenize_unique(normalized_query)
    if not original_tokens:
        return []
    tokens, weights = expand_query_weighted(original_tokens, normalized_query)
    if not tokens:
        return []

    totals = view.corpus_stats()
    if totals.total_chunks <= 0:
        return []
    owners = view.documents()
    summaries = view.token_stats(tokens)
    effective_df = {
        token: _effective_df(view, summaries.get(token), totals.total_chunks)
        for token in tokens
    }

    postings_by_ref: dict[ChunkRef, dict[str, SearchPosting]] = {}
    for token in tokens:
        token_rows = sorted(
            view.iter_effective_postings(token),
            key=lambda posting: (
                owners[posting.chunk_ref.doc_uid].doc_key,
                posting.chunk_ref.local_id,
            ),
        )
        if len(token_rows) != effective_df[token]:
            raise PinnedSearchViewError(
                f"effective posting count differs from DF for {token!r}"
            )
        for posting in token_rows:
            postings_by_ref.setdefault(posting.chunk_ref, {})[token] = posting
    if not postings_by_ref:
        return []

    # Preserve the legacy candidate insertion order: query token order, then
    # compiler-global chunk order within each posting list.  Stable score ties
    # therefore resolve exactly as they did for numeric CIDs.
    candidate_refs = tuple(postings_by_ref)
    metrics = view.get_chunk_metrics(candidate_refs)
    field_averages = {
        "title": totals.title_length_sum / totals.total_chunks,
        "breadcrumb": totals.breadcrumb_length_sum / totals.total_chunks,
        "body": totals.body_length_sum / totals.total_chunks,
    }
    average_length = sum(field_averages.values())
    scored: list[tuple[ChunkRef, float]] = []
    for ref in candidate_refs:
        score = _bm25_score(
            tokens,
            weights,
            postings_by_ref[ref],
            metrics[ref],
            effective_df=effective_df,
            total_chunks=totals.total_chunks,
            field_averages=field_averages,
            average_length=average_length,
        )
        if score > 0:
            scored.append((ref, _js_round(score, 2)))
    scored.sort(key=lambda item: item[1], reverse=True)

    path_b_refs = _truncate_scored_refs(scored, owners, max(top_k, 50))
    path_a_refs = _truncate_scored_refs(scored, owners, 60)
    routed_doc_uids = _route_documents(
        original_tokens,
        {token: 1.0 for token in original_tokens},
        postings_by_ref,
        owners,
    )
    route_scan_refs = _ordered_refs(
        _document_refs(view, routed_doc_uids), owners
    )[:60]
    path_e_refs = route_scan_refs[:20]

    # Body text and position tokenization are needed only after ranking. A
    # broad posting list may require compact metrics for every candidate, but
    # it must never hydrate every losing Segment chunk.
    query_refs = tuple(dict.fromkeys((*path_b_refs, *path_a_refs)))
    hydrated_refs = tuple(dict.fromkeys((*query_refs, *path_e_refs)))
    raw_chunks = view.get_chunks(hydrated_refs)
    chunks = {
        ref: _decorate_chunk(raw_chunks[ref], ref, owners[ref.doc_uid], view)
        for ref in hydrated_refs
    }
    score_by_ref = dict(scored)
    query_hits = {
        ref: _hit_from_chunk(
            chunks[ref],
            score=score_by_ref[ref],
            tokens=tokens,
            positions=_positions(tokens, chunks[ref], postings_by_ref[ref]),
        )
        for ref in query_refs
    }
    path_b = [query_hits[ref] for ref in path_b_refs]
    path_a_candidates = [query_hits[ref] for ref in path_a_refs]
    path_a = _phrase_path(normalized_query, path_a_candidates, 20)

    path_e: list[Hit] = []
    for ref in path_e_refs:
        chunk = chunks[ref]
        path_e.append(
            _hit_from_chunk(
                chunk,
                score=0.1,
                tokens=original_tokens,
                positions={},
            )
        )

    fused = rrf_fuse([path_a, path_b, path_e])
    return _per_doc_truncate(fused, top_k)
