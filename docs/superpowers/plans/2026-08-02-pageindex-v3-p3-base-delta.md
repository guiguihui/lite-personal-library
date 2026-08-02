# PageIndex v3 P3 Base + Delta Search View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make incremental indexing proportional to changed documents by replacing the default schema-3 monolith rebuild with immutable logical Generations and stable-reference base+delta Search Views, while keeping full optimization and legacy export explicit user choices.

**Architecture:** A logical Generation identifies `doc_key -> segment_hash` under a logical search recipe. A physical Search View independently identifies one immutable base posting layer plus ordered document-replacement deltas; raw title, breadcrumb, and body TF remain stored, while extreme body-DF suppression is applied from pinned global statistics at query time. Normal incremental builds hash the source catalog once, build only changed Segments, write one small delta and one small View, validate only new artifacts and parent attestations, and never invoke the schema-3 compatibility compiler.

**Tech Stack:** Python 3.10+, standard library only (`ctypes`, `dataclasses`, `errno`, `hashlib`, `heapq`, `json`, `os`, `pathlib`, `struct`, `sys`, `tempfile`, `threading`), existing PageIndex v2 Segment/object-store/canonical primitives, FastAPI retrieval code, pytest, Windows PSAPI benchmark harness.

## Scope Boundary

This plan ends with a shadow-capable pinned reader, query adapter, P3 worker, explicit optimize/legacy-export commands, and exact-50k performance/equivalence evidence. Formal `current.json` CAS publication, rollback leases, chat migration, delayed GC, and removal of root monolith runtime dependencies are a separate P4 cutover plan because they coordinate the HTTP server, frontend session lifecycle, and storage retention policy independently of the P3 indexing algorithm.

Concurrent/cooperative builders and untrusted on-disk artifacts are in scope. A hostile process running as the same OS user and mutating a freshly randomized private staging directory between individual filesystem syscalls is not a security boundary; static symlink/junction paths and any observed directory-identity drift still fail closed, and cleanup never recursively traverses an unexpected replacement.

## Global Constraints

- Incremental is the default path. Full base optimization and legacy schema-3 export are explicit options and never run automatically inside a dirty build.
- No database, resident search service, message queue, or new third-party runtime dependency.
- Preserve immutable content-addressed v2 Segments; add search summaries as sidecars instead of changing Segment schema 2.
- `doc_uid = SHA-256(UTF-8(doc_key))`; chunk identity is `(doc_uid, segment_hash, local_id)`. P3 never assigns a Generation-global numeric chunk ID.
- Title and breadcrumb TF are always query-visible. Body TF is query-visible except when `df_body >= 256` and `df_body * 10 >= total_chunks * 9`; the recipe stores numerator/denominator integers, never a floating-point threshold.
- Base and delta layers store raw field TF. Threshold crossings in either direction must not rewrite postings.
- Token statistics preserve `df_any`, `df_nonbody`, and `df_body`. When body is suppressed, IDF uses `df_nonbody`; otherwise it uses `df_any`.
- P3 Generation, base, delta, and View identities are the complete 64-character lowercase SHA-256. A short prefix may be shown in logs/UI, but is never accepted as an internal identity or path key.
- Logical Generation identity contains logical recipe and `doc_key -> segment_hash`, never View layout, compaction history, task IDs, timestamps, absolute paths, or legacy export settings.
- View identity contains Generation, physical recipe, base/delta object identities, and attested small runtime artifacts.
- A pinned reader resolves `{generation, view_id}` once and never rereads `current.json` or chooses a latest-mtime fallback during a request.
- Normal dirty validation may load the old and new Segment for each changed document, but may not enumerate or parse the full base posting layer, call `app.index.v2.compiler.compile_generation`, or call `compile_generation_to_candidate`.
- A failed, cancelled, or concurrent-loser build leaves all published pointers and immutable parent objects unchanged.
- Exact-50k gates: edit/delete P95 `<5 s`, no-op P95 `<500 ms`, Peak Working Set and Private Bytes `<512 MiB`, query P95 regression `<10%`, and dirty `postings_visited`/bytes written proportional to changed documents.

---

### Task 1: Separate logical, physical, policy, and compatibility identities

**Files:**
- Create: `app/index/v3/__init__.py`
- Create: `app/index/v3/models.py`
- Create: `tests/pageindex_v3/test_models.py`

**Interfaces:**
- Consumes: `app.index.v2.canonical.canonical_hash`, `app.index.v2.ids.make_doc_key`, and structurally validated v2 `StoredSegmentRef` values.
- Produces: strict `validate_doc_key()`/`validate_sha256()`, `make_doc_uid()`, `logical_generation_core()`, `logical_generation_id()`, `GenerationRecipe`, `SearchViewRecipe`, `CompactionPolicy`, `LegacyExportRecipe`, `ViewPin`, `ChunkRef`, logical `SearchPosting`, physical `LayerPosting`, `TokenSummary`, and `SegmentSummary`.

- [x] **Step 1: Write failing identity, recipe, and value-object tests**

Cover exact recipe dictionaries, canonical serializability, frozen/slotted records, detached `as_dict()` results, unsupported schema/artifact/codec versions, bool counts, non-finite/out-of-range policy values, malformed/non-NFC document keys, malformed/uppercase/short hashes, same slug across types, and all integer boundaries through `2**64 - 1`.

```python
def test_logical_identity_is_order_independent_and_physically_isolated():
    refs = (stored_ref("note:a", "1" * 64), stored_ref("book:a", "2" * 64))
    core = logical_generation_core(refs, GenerationRecipe())
    assert set(core) == {
        "artifact_kind", "schema_version", "generation_recipe_hash", "documents"
    }
    assert logical_generation_id(refs, GenerationRecipe()) == canonical_hash(core)
    assert logical_generation_id(tuple(reversed(refs)), GenerationRecipe()) == canonical_hash(core)
    assert len(logical_generation_id(refs, GenerationRecipe())) == 64

def test_doc_uid_is_type_namespaced_and_portable():
    assert make_doc_uid("note:a") != make_doc_uid("book:a")
    assert make_doc_uid("note:a") == hashlib.sha256(b"note:a").hexdigest()
```

- [x] **Step 2: Run the tests and confirm imports fail**

Run: `python -m pytest tests/pageindex_v3/test_models.py -q`
Expected: collection fails because `app.index.v3.models` does not exist.

- [x] **Step 3: Implement strict frozen recipes and keep policy out of physical identity**

```python
@dataclass(frozen=True, slots=True)
class GenerationRecipe:
    schema_version: int = 1
    artifact_kind: str = "logical_generation_recipe"
    field_postings_version: str = "raw-field-tf-v1"
    chunk_ref_version: str = "doc-uid-segment-local-v1"
    idf_policy_version: str = "effective-df-v1"
    body_df_min: int = 256
    body_df_ratio_numerator: int = 9
    body_df_ratio_denominator: int = 10

@dataclass(frozen=True, slots=True)
class SearchViewRecipe:
    schema_version: int = 1
    artifact_kind: str = "search_view_recipe"
    posting_codec_version: str = "piv3-split-field-uvarint-v1"
    chunk_lengths_codec_version: str = "piv3-document-block-uvarint-v1"
    term_index_version: str = "canonical-jsonl-sparse-v1"
    replacement_version: str = "document-newest-wins-v1"
    owner_map_version: str = "layer-owner-map-v1"
    statistics_version: str = "scalar-plus-layer-delta-v1"

@dataclass(frozen=True, slots=True)
class CompactionPolicy:
    max_delta_layers: int = 32
    max_delta_bytes_numerator: int = 1
    max_delta_bytes_denominator: int = 5
```

`CompactionPolicy` only determines `compaction_recommended`; it is never included in Generation, base, delta, or View identity. `LegacyExportRecipe` copies the supported legacy format/order/layout constants rather than embedding a mutable `CompilerRecipe`. All rational thresholds are reduced by GCD before equality, serialization, or hashing so equivalent fractions have one identity.

- [x] **Step 4: Implement stable identities and posting/summary records**

`logical_generation_core()` validates every manually constructible `StoredSegmentRef` without stat/loading: canonical doc identity, matching `doc_type/slug`, lowercase SHA-256 fields, non-bool non-negative byte size, and unique doc keys/segment hashes. Identity projects only sorted `doc_key -> segment_hash`; path, byte size, source content hash, source recipe hash, source proof, task metadata, Search View recipe, compaction policy, and legacy recipe are excluded.

`SearchPosting` carries `ChunkRef(doc_uid, segment_hash, local_id)`; `LayerPosting` carries layer-local `doc_ordinal/local_id`. Both retain raw title/breadcrumb/body TF and reject zero-total rows. `TokenSummary` stores `df_any/df_nonbody/df_body` and enforces `max(df_nonbody, df_body) <= df_any <= df_nonbody + df_body`. `SegmentSummary` stores immutable sorted token tuples, scalar field-length/posting totals, complete Segment attestation, and validates `posting_count == sum(df_any)`.

- [x] **Step 5: Run focused and compatibility tests**

Run:

```powershell
python -m pytest tests/pageindex_v3/test_models.py -q
python -m pytest tests/pageindex_v2/test_canonical_ids.py tests/pageindex_v2/test_object_store.py tests/pageindex_v2/test_compiler.py tests/pageindex_v3/test_models.py -q
```

Expected: all pass; changing Generation body/IDF policy changes logical identity, while changing physical, compaction, or legacy recipes cannot enter that API.

- [x] **Step 6: Commit**

```powershell
git add app/index/v3/__init__.py app/index/v3/models.py tests/pageindex_v3/test_models.py
git commit -m "refactor(pageindex): separate logical and physical identities"
```

### Task 2: Capture one stable catalog snapshot and derive an explicit change set

**Files:**
- Modify: `app/index/v2/source_snapshot.py`
- Create: `app/index/v3/source_diff.py`
- Create: `tests/pageindex_v3/test_source_diff.py`
- Verify: `tests/pageindex_v2/test_source_snapshot.py`

**Interfaces:**
- Consumes: v2 `DocumentSource`, input-proof format, base `StoredSegmentRef` mapping, cancellation callback.
- Produces: `StableCatalogSnapshot(proof, sources, file_state, topology, directory_state)`, `capture_stable_catalog(...)`, `StableCatalogSnapshot.verify_unchanged()`, and `SegmentChangeSet(base_by_doc, current_fingerprints, added, changed, deleted, unchanged)`.

- [x] **Step 1: Add tests proving capture hashes every source once and exposes only dirty keys for later body rereads**

```python
def test_change_set_reuses_snapshot_without_second_full_content_hash(monkeypatch, corpus):
    reads = Counter()
    monkeypatch.setattr(snapshot_module, "_hash_open_file", counting_hash(reads))
    snapshot = capture_stable_catalog(corpus.root, ...)
    changes = diff_segment_inputs(snapshot, corpus.base_refs)
    assert changes.changed == ("note:changed",)
    assert set(reads.values()) == {1}
    assert snapshot.verify_unchanged()
```

Cover add, edit, delete, ABA metadata changes, topology changes, ancestor junction/symlink escape, final-file symlink, cancellation, and deterministic doc ordering.

- [x] **Step 2: Run focused tests and confirm the new API is absent**

Run: `python -m pytest tests/pageindex_v3/test_source_diff.py tests/pageindex_v2/test_source_snapshot.py -q`
Expected: P3 tests fail on missing `capture_stable_catalog`.

- [x] **Step 3: Refactor the existing proof capture into a reusable snapshot**

```python
@dataclass(frozen=True, slots=True)
class StableCatalogSnapshot:
    content_dir: Path
    sources: tuple[DocumentSource, ...]
    proof: dict[str, object]
    directory_state: _DirectoryState
    topology: _Topology
    file_state: tuple[_FileState, ...]

    def verify_unchanged(self, check_cancel=lambda: None) -> bool:
        return (
            _catalog_directory_state(self.content_dir) == self.directory_state
            and _rescan_catalog_topology(self.content_dir) == self.topology
            and _file_state(_prepare_sources(self.content_dir, self.sources))
            == self.file_state
        )
```

Keep `capture_stable_input_proof()` as a compatibility wrapper returning `snapshot.proof`. Verification must compare stat/topology envelopes and must not hash file contents again.

- [x] **Step 4: Implement `SegmentChangeSet` without loading Segments**

`diff_segment_inputs()` compares snapshot `content_hash` values with base ref attestations. It returns sorted document-key tuples and rejects duplicate/missing proof entries. It does not call `load_segment()`.

- [x] **Step 5: Run v2 and P3 snapshot tests**

Run: `python -m pytest tests/pageindex_v2/test_source_snapshot.py tests/pageindex_v2/test_no_change.py tests/pageindex_v3/test_source_diff.py -q`
Expected: all tests pass and the v2 no-op proof bytes are unchanged.

- [x] **Step 6: Commit**

```powershell
git add app/index/v2/source_snapshot.py app/index/v3/source_diff.py tests/pageindex_v2/test_source_snapshot.py tests/pageindex_v3/test_source_diff.py
git commit -m "refactor(pageindex): reuse one stable source snapshot"
```

### Task 3: Project raw Segment facts and persist summary sidecars

**Files:**
- Create: `app/index/v3/segment_projection.py`
- Create: `app/index/v3/summary_store.py`
- Modify: `app/index/v3/__init__.py`
- Create: `tests/pageindex_v3/test_segment_projection.py`
- Create: `tests/pageindex_v3/test_summary_store.py`

**Interfaces:**
- Consumes: `StoredSegmentRef`, `load_segment()`, `SegmentRecipe`, the v2 tokenizer, and Task 1 models.
- Produces: `ChunkMetric`, detached `SegmentProjection`, `StoredSummaryRef`, `SegmentProjector.project(ref)`, allocation-bounded `summarize(ref)`, streaming `iter_postings(ref)` and `project_to_sink(ref, consume_posting)`, `load_chunks(ref, local_ids)`, `put_summary(pageindex_dir, summary) -> StoredSummaryRef`, and `load_summary(pageindex_dir, ref, summary_ref)`.

- [x] **Step 1: Write projection oracle tests**

```python
def test_projection_keeps_all_field_tf_and_stable_chunk_refs(pageindex, ref):
    projection = SegmentProjector(pageindex).project(ref)
    assert projection.postings == tuple(sorted(projection.postings))
    assert any(row.title_tf and not row.body_tf for row in projection.postings)
    assert any(row.breadcrumb_tf and not row.body_tf for row in projection.postings)
    assert any(row.body_tf for row in projection.postings)
    assert all(
        row.chunk_ref.doc_uid == make_doc_uid(ref.doc_key)
        for row in projection.postings
    )
```

Assert `df_any`, `df_nonbody`, `df_body`, exact re-tokenized field lengths/postings, compact local IDs, posting count, duplicate rejection, empty fields, Unicode tokens, same slug across different types, and that body TF remains present even above the pruning threshold.

- [x] **Step 2: Run tests and confirm missing projector/store failures**

Run: `python -m pytest tests/pageindex_v3/test_segment_projection.py tests/pageindex_v3/test_summary_store.py -q`
Observed: both imports failed before implementation.

- [x] **Step 3: Implement one-Segment projection**

```python
@dataclass(frozen=True, slots=True)
class SegmentProjection:
    ref: StoredSegmentRef
    summary: SegmentSummary
    postings: tuple[SearchPosting, ...]
    chunk_metrics: tuple[ChunkMetric, ...]

class SegmentProjector:
    def project_to_sink(self, ref, consume_posting):
        # validate once; emit one SearchPosting at a time
        ...
```

The private analyzer performs validator-level checks in one decoded Segment: exact recipe/source fingerprints, unique nodes, compact chunk IDs, node references, strict field types and lengths, complete raw postings re-derived one chunk at a time with the v2 tokenizer, and stable UTF-8 ordering without cloning the posting table. It computes union `df_any`, `df_nonbody`, and `df_body` and never invokes body pruning. `summarize()` builds no `SearchPosting` rows; `iter_postings()` and `project_to_sink()` retain no posting tuple; `SegmentProjection` uses adjacent ordering checks instead of full key/sort/set copies. Task 4/6 builders must use `project_to_sink()` rather than `project()`.

- [x] **Step 4: Implement canonical authenticated summary sidecars**

Store summaries at `objects/search/summaries/<segment_hash[:2]>/<segment_hash>.json`. Encoding and exact-byte comparison stream scalar fields and one token record at a time; they never call `canonical_bytes()` or build `summary.as_dict()` for the full token table. `put_summary()` returns a strict `StoredSummaryRef` containing SHA-256, byte size, and complete Segment identity. A trusted View/owner manifest must retain that receipt; `load_summary()` requires it and streams file hash/size verification before parsing and re-hashes the strict parsed semantics against the same receipt, so canonical-but-wrong statistics and hash/parse replacement races cannot silently rebind themselves.

Publication uses a same-directory fsynced temporary plus exclusive hard-link install. Concurrent identical writers converge while corruption/conflicts are never overwritten. On Windows filesystems without hard-link support, same-directory `os.rename` provides atomic no-clobber fallback; POSIX never falls back to an overwriting rename. Loads additionally require canonical JSON, exact keys, strict model invariants, no symlink escape, and complete ref identity binding.

- [x] **Step 5: Verify one-live-Segment ownership and deterministic sidecars**

Run: `python -m pytest tests/pageindex_v3/test_segment_projection.py tests/pageindex_v3/test_summary_store.py -q`
Observed: 59 passed, 1 skipped; full suite 630 passed, 4 skipped. Tests cover summary-only/no-posting materialization, one-row-at-a-time sink output, lazy posting iteration, adjacent validation, trusted receipt tampering, Windows hard-link fallback, exact idempotence, conflicts, and weak-reference Segment release.

- [x] **Step 6: Commit**

```powershell
git add app/index/v3/__init__.py app/index/v3/segment_projection.py app/index/v3/summary_store.py tests/pageindex_v3/test_segment_projection.py tests/pageindex_v3/test_summary_store.py docs/superpowers/plans/2026-08-02-pageindex-v3-p3-base-delta.md
git commit -m "feat(pageindex): persist raw search projections"
```

### Task 4: Add seekable PIV3/PCV posting layers with a sparse term index

**Files:**
- Create: `app/index/v3/varint.py`
- Create: `app/index/v3/layer_codec.py`
- Create: `app/index/v3/layer_runs.py`
- Create: `tests/pageindex_v3/test_varint.py`
- Create: `tests/pageindex_v3/test_layer_codec.py`
- Create: `tests/pageindex_v3/test_layer_runs.py`
- Create: `tests/pageindex_v3/test_staged_layer_builder.py`

**Interfaces:**
- Consumes: sorted logical `SearchPosting` records, layer-local `LayerPosting` rows, a canonical layer document table, signed token-statistic contributions, and P2 `AtomicHashingSink`/`ArtifactRef` primitives.
- Produces: `PostingLayerReceipt`, `write_posting_layer(..., check_cancelled)`, `PostingLayerReader.iter_token(token, include_body=True)`, `PostingLayerReader.get_chunk_metrics(refs)`, explicit `PostingLayerReader.audit()`, `StagedLayerBuilder`, and compatibility `build_sorted_layer(..., max_run_bytes, merge_fan_in, check_cancelled)`.

- [x] **Step 1: Lock the layer document table and PIV3 bytes**

`layer-documents.json` assigns dense layer-local ordinals by a list strictly sorted by `doc_uid`. Every record contains `doc_key, doc_uid, segment_hash, chunk_count, chunk_block_offset, chunk_block_bytes, chunk_block_sha256`. Base layers contain every document; delta layers contain only added/changed new versions. Ordinals are physical compression IDs and never appear in public/cache identity. The local chunk-block digest lets a dirty build or query authenticate one candidate document without hashing the complete PCV artifact.

`postings.piv` starts with `b"PIV3PST1"`. Token groups are strictly increasing by UTF-8 token bytes:

```text
u32be token_utf8_bytes
token_utf8
u64be nonbody_row_count
repeated nonbody rows sorted by (doc_ordinal, local_id):
    minimal-uvarint doc_ordinal
    minimal-uvarint local_id
    minimal-uvarint title_tf
    minimal-uvarint breadcrumb_tf
u64be body_row_count
repeated body rows sorted by (doc_ordinal, local_id):
    minimal-uvarint doc_ordinal
    minimal-uvarint local_id
    minimal-uvarint body_tf
```

Non-body rows require `title_tf + breadcrumb_tf > 0`; body rows require `body_tf > 0`. A chunk may occur once in both partitions. Term metadata bounds the complete group exactly, so a pruned query reads the non-body partition and seeks past body bytes without decoding them.

- [x] **Step 2: Lock candidate-seekable chunk metrics**

`chunks.pcv` starts with `b"PIV3CHK1"` and stores one document block per layer document:

```text
minimal-uvarint doc_ordinal
minimal-uvarint chunk_count
repeated chunks with strictly increasing local_id:
    minimal-uvarint local_id
    minimal-uvarint title_length
    minimal-uvarint breadcrumb_length
    minimal-uvarint body_length
```

The document table's offset/size/digest exactly authenticates and encloses its block. PCV copies no title, breadcrumb, or body text; the reader seeks and hashes only candidate-document blocks, and Segment hydration happens after ranking.

- [x] **Step 3: Lock canonical term metadata and bounded lookup**

`terms.jsonl` is `canonical_bytes(record) + b"\n"`, strictly sorted by token. Each record contains `token, block_offset, block_bytes, nonbody_rows, body_rows, df_any_delta, df_nonbody_delta, df_body_delta, prefix_bytes, prefix_sha256, body_offset, body_bytes, body_sha256`. The prefix digest authenticates token/non-body rows/body-count independently; the body digest lets `include_body=False` skip body bytes physically. Base contributions are non-negative; delta contributions are signed. A token with postings but zero net DF change remains; a disappeared token remains with null/zero block and negative statistics; a record with no postings and an all-zero triple is forbidden.

`terms.sidx.json` is canonical, declares stride 128 and the terms artifact SHA-256/size/line count, and stores `[first_token, byte_offset, window_bytes, window_sha256, line_count]` for each contiguous window. Lookup binary-searches the sparse array, reads and authenticates exactly one window, and scans at most 128 canonical lines. It never materializes or hashes the complete lexicon on the normal hot path.

- [x] **Step 4: Add corruption, random-seek, and body-skip tests**

Cover bad magic, truncation, overlong/non-minimal varints, invalid UTF-8, integer overflow, unknown ordinal, duplicate/non-monotonic rows, zero TF, invalid PCV block boundaries, overlapping/out-of-range token blocks, token mismatch, noncanonical JSONL, invalid signed triples, sparse-index digest/offset/stride mismatch, extra bytes, and Windows handle closure. Instrument reads to prove one-token lookup touches only one sparse window and one token group; `include_body=False` must not read body row bytes.

- [x] **Step 5: Implement strict streaming readers/writers and receipts**

Every integer rejects `bool`, negatives, and values above `2**64 - 1`; decoded varints are re-encoded to prove minimality. Readers bind ordinals through the attested document table and restore complete `ChunkRef` values. Writers stream PIV3, PCV, JSONL, sparse index, and SHA-256 receipts without creating whole-artifact strings/byte arrays, and check cancellation inside documents, very-high-DF token groups, spool copies, and term windows.

`PostingLayerReceipt` attests the five artifacts plus document/chunk/term/nonbody/body counts and the physical Search View recipe. It is the only supported way to open a layer. Opening pins all five file handles and authenticates only the small document/sparse routing metadata; local window/PIV/PCV digests protect random reads. Full artifact SHA-256 and semantic revalidation are explicit `audit()` work and never run on no-op or normal dirty builds.

- [x] **Step 6: Implement encoded-byte-bounded external runs**

`LayerRunBuilder` accounts before append for both encoded bytes and a conservative resident charge based on the actual Python row/string/key representations (including PEP 393 non-BMP expansion), sorts in place, spills before either configured bound, merges with at most `merge_fan_in` readers plus one writer, uses counted SHA-256-footer scratch runs, unique scratch directories, strict cleanup, and closes all Windows handles before cleanup. Ordering is `(token_utf8, doc_ordinal, local_id)`; duplicate keys fail deterministically. `StagedLayerBuilder.begin_document()` assigns the physical ordinal before projection, its ticket streams postings directly into bounded runs, and `ticket.commit(chunk_count, chunk_metrics)` appends one PCV block plus a disk-spooled document record. It retains only one Segment's metrics and O(documents) lean routing/chunk-count metadata; `build_sorted_layer()` is a compatibility wrapper, not the Task 6 production path.

- [x] **Step 7: Run codec/run tests under forced one-row runs and two-way fan-in**

Run: `python -m pytest tests/pageindex_v3/test_varint.py tests/pageindex_v3/test_layer_codec.py tests/pageindex_v3/test_layer_runs.py tests/pageindex_v3/test_staged_layer_builder.py -q`
Expected: deterministic bytes independent of input order; observed readers, run bytes, sparse scans, candidate-PCV reads, and body-partition reads stay within their bounds.

- [x] **Step 8: Commit**

```powershell
git add app/index/v3/__init__.py app/index/v3/varint.py app/index/v3/layer_codec.py app/index/v3/layer_runs.py tests/pageindex_v3/test_varint.py tests/pageindex_v3/test_layer_codec.py tests/pageindex_v3/test_layer_runs.py tests/pageindex_v3/test_staged_layer_builder.py docs/superpowers/plans/2026-08-02-pageindex-v3-p3-base-delta.md
git commit -m "feat(pageindex): add seekable posting layers"
```

### Task 5: Build logical Generations, scalar totals, and reversible token contributions

**Files:**
- Modify: `app/index/v3/__init__.py`
- Create: `app/index/v3/generation.py`
- Create: `app/index/v3/statistics.py`
- Create: `tests/pageindex_v3/test_generation.py`
- Create: `tests/pageindex_v3/test_statistics.py`

**Interfaces:**
- Consumes: Task 1 recipes/models, Segment refs, Task 3 summaries, P1 input proof.
- Produces: compact `LogicalGenerationReceipt`, `build_logical_generation(refs, proof, recipe, candidate_dir, check_cancelled)`, strict manifest validation, `CorpusTotals.from_summaries(..., token_count)`, `CorpusTotals.apply(removed, added, token_count_delta)`, and per-token signed `TokenDfDelta` values for changed summaries.

- [x] **Step 1: Test stable logical identity and artifact discrimination**

```python
def test_full_incremental_and_recompile_have_one_logical_generation_id(refs, proof):
    a = build_logical_generation(refs, proof, GenerationRecipe(), path_a)
    b = build_logical_generation(tuple(reversed(refs)), proof, GenerationRecipe(), path_b)
    assert a.generation_id == b.generation_id
    assert len(a.generation_id) == 64
    assert a.manifest_ref.sha256 == b.manifest_ref.sha256
    assert not hasattr(a, "manifest")  # receipt never retains the O(N) mapping
```

Ensure old schema-2/3 compatibility manifests cannot be parsed as P3 logical manifests even if their `generation` field looks valid. Reject short hash prefixes everywhere except explicit display helpers.

- [x] **Step 2: Test exact `base - old + new` arithmetic without a full vocabulary rewrite**

Build scalar totals from several summaries, replace one, delete one, and add one. Assert patched totals equal a clean recomputation and negative intermediate values fail. Independently derive sorted signed token triples `[df_any, df_nonbody, df_body]` from only removed/added summaries; zero triples disappear, token-only negative deltas remain, and threshold crossings preserve all three raw values.

- [x] **Step 3: Implement small canonical Generation artifacts**

The core manifest is:

```python
core = {
    "artifact_kind": "logical_generation",
    "schema_version": 4,
    "generation_recipe_hash": canonical_hash(recipe.as_dict()),
    "documents": {ref.doc_key: ref.segment_hash for ref in sorted_refs},
}
generation_id = canonical_hash(core)
```

Write canonical `manifest.json` and `input-proof.json` incrementally inside an identity-checked private sibling staging directory, then publish the complete candidate with an atomic no-replace rename (Windows native rename or Linux `renameat2(RENAME_NOREPLACE)`; unsupported platforms fail closed); include the recipe, complete Generation ID, proof/artifact digests and sizes, and document count. Compute every O(N) hash with `iter_canonical_json()` plus incremental SHA-256—never `canonical_bytes()`—and keep only compact ArtifactRefs in `LogicalGenerationReceipt` rather than a third in-memory manifest/documents mapping. Source proof is an attestation excluded from the logical identity core but must exactly bind the recipe plus every ref's content and Segment-recipe hashes. Do not include Search View layout, corpus statistics, task metadata, or legacy export fields.

- [x] **Step 4: Implement immutable scalar totals and sparse token deltas**

`CorpusTotals` contains only `documents`, `total_chunks`, `token_count`, three field-length sums, and `posting_count`; its canonical size is O(1). `posting_count` always comes from `SegmentSummary.posting_count`, never Task 4 `postings.records` because split-field rows can double-count one logical posting. Full-base construction requires token_count from the base term merge. Incremental `apply()` first proves every `base - removed` scalar is non-negative, then adds new summaries, applies the touched-token-derived `token_count_delta`, checks overflow/conservation, and never infers token_count from a delta layer's physical term_count.

`token_df_deltas(removed, added)` reads only the changed summaries and emits sorted signed `TokenDfDelta(token, df_any, df_nonbody, df_body)`. It never loads or materializes the base vocabulary; all-zero net triples disappear, negative-only token disappearance and field-only migration remain. The statistics domain does not import Task 4's physical codec; Task 7 adapts deltas lazily to `TokenContribution`. Full-base token contributions are produced during the external posting merge and stored in Task 4 `terms.jsonl`, not in a View-wide `statistics.json`.

- [x] **Step 5: Run tests**

Run: `python -m pytest tests/pageindex_v3/test_generation.py tests/pageindex_v3/test_statistics.py -q`
Result: `123 passed, 1 skipped`; clean/patched scalar totals match exactly, and one-document token-delta work is bounded by that document's summary.

- [x] **Step 6: Commit**

```powershell
git add app/index/v3/__init__.py app/index/v3/generation.py app/index/v3/statistics.py tests/pageindex_v3/test_generation.py tests/pageindex_v3/test_statistics.py docs/superpowers/plans/2026-08-02-pageindex-v3-p3-base-delta.md
git commit -m "feat(pageindex): add logical generations and search statistics"
```

### Task 6: Build immutable full base Search Views

**Files:**
- Modify: `app/index/v3/__init__.py`
- Modify: `app/index/v3/generation.py`
- Create: `app/index/v3/base_builder.py`
- Create: `app/index/v3/view_store.py`
- Modify: `tests/pageindex_v3/test_generation.py`
- Create: `tests/pageindex_v3/test_base_builder.py`
- Create: `tests/pageindex_v3/test_view_store.py`
- Modify: `docs/superpowers/plans/2026-08-02-pageindex-v3-p3-base-delta.md`

**Interfaces:**
- Consumes: ordered refs, Task 3 projections/sidecars, Task 4 layer builder, Task 5 Generation/totals, and Task 1 physical recipe.
- Produces: `BaseObjectReceipt`, `SearchViewReceipt`, `build_base_view(...)`, and strict content-addressed finalize/load functions.

- [x] **Step 1: Write a full-base oracle test**

Build a rich three-document base twice with reversed refs and forced multi-level runs. Assert exact 64-hex base/View IDs, artifact bytes, raw field postings, token triples, scalar totals, owner map, PCV metrics, and stable ChunkRefs.

- [x] **Step 2: Freeze base and View artifacts/manifests**

A base lives at `objects/search/bases/<base_id>/` and contains `layer-documents.json`, `postings.piv`, `chunks.pcv`, `terms.jsonl`, `terms.sidx.json`, and `manifest.json`. Its core binds the target Generation ID and manifest SHA-256, Search View recipe hash, every layer receipt, and scalar statistics. `base_id = canonical_hash(base_core)`.

A View lives at `views/<view_id>/` and contains canonical `manifest.json`, O(1) `statistics.json`, and document-level `documents.json`. Statistics contains only `documents, total_chunks, token_count, posting_count` and the three field-length sums; never a token map. Documents is an active owner map keyed by `doc_uid`, with `doc_key, segment_hash, summary_sha256, summary_bytes, owner_layer_kind, owner_layer_id, doc_ordinal`; deleted documents are absent. The trusted summary receipt is physical validation metadata and is excluded from logical Generation identity.

```python
view_core = {
    "artifact_kind": "search_view",
    "schema_version": 1,
    "generation": generation.generation_id,
    "generation_manifest_sha256": generation.manifest_ref.sha256,
    "search_view_recipe_hash": canonical_hash(recipe.as_dict()),
    "base_id": base.base_id,
    "delta_ids": [],  # oldest to newest; never sorted after construction
    "statistics_sha256": statistics_ref.sha256,
    "documents_sha256": documents_ref.sha256,
}
view_id = canonical_hash(view_core)
```

- [x] **Step 3: Implement a one-Segment-at-a-time full base build**

For each ref, call `StagedLayerBuilder.begin_document()` to assign the ordinal, call `project_to_sink()` exactly once with `ticket.add_posting` as its sink, persist/reuse the summary and retain only its `StoredSummaryRef`, then `ticket.commit(summary.chunk_count, metrics)` to append PCV facts. Update scalar totals/document owners with the trusted summary SHA/size and release the summary, metrics, and all Segment-derived containers before the next ref. The external merge emits base-positive term contributions and token_count. Do not call compatibility `build_sorted_layer()`, reload a Segment for metrics, or write a legacy export.

Before projection, `validate_logical_generation_inputs()` consumes the Segment refs once, recomputes the logical Generation/proof/manifest attestations incrementally, authenticates the exact two Generation files, and returns only the original lean refs in canonical `doc_key` order. The builder then reorders that one lean list by `doc_uid`; it never materializes an O(N) decoded Generation document map.

- [x] **Step 4: Implement content-addressed finalization**

Existing base/View objects are accepted only when their canonical manifest and every attested artifact digest/size/count match. Concurrent identical finalization reuses the object; a mismatch retains the candidate and fails. Candidate ownership and Windows handle closure follow the P2 receipt finalizer.

Every pre-existing destination ancestor is `lstat`-checked and POSIX symlinks/Windows reparse points are rejected; missing parents are created one level at a time and the final parent identity is rechecked immediately before atomic no-replace publication. As in Task 5, a hostile same-user process replacing a directory in the gap between individual syscalls is outside the local-store threat boundary; any observed identity drift fails closed.

- [x] **Step 5: Run base/View tests and bounded-ownership assertions**

Run: `python -m pytest tests/pageindex_v3/test_base_builder.py tests/pageindex_v3/test_view_store.py -q`
Expected: deterministic output, `segments_loaded_peak <= 1`, bounded run/fan-in use, full owner-map agreement with the logical Generation, and no schema-3 artifact names.
Result: `23 passed, 1 skipped`; the complete repository regression is `858 passed, 6 skipped`.

- [x] **Step 6: Commit**

```powershell
git add app/index/v3/__init__.py app/index/v3/generation.py app/index/v3/base_builder.py app/index/v3/view_store.py tests/pageindex_v3/test_generation.py tests/pageindex_v3/test_base_builder.py tests/pageindex_v3/test_view_store.py docs/superpowers/plans/2026-08-02-pageindex-v3-p3-base-delta.md
git commit -m "feat(pageindex): build immutable base search views"
```

### Task 7: Write document-replacement deltas without touching base postings

**Files:**
- Create: `app/index/v3/delta_builder.py`
- Create: `tests/pageindex_v3/test_delta_builder.py`

**Interfaces:**
- Consumes: a parent pinned View, `SegmentChangeSet`, old/new Segment refs, trusted `StoredSummaryRef` values and summaries, touched-token lookups, Task 4 layer codec, Task 5 totals, and a non-identifying `CompactionPolicy`.
- Produces: `DeltaObjectReceipt` and `build_delta_view(parent, generation, changes, ...) -> SearchViewReceipt` plus a separate compaction recommendation.

- [ ] **Step 1: Write add/edit/delete/token-disappearance tests**

Cover multiple changed docs, same slug across types, deletion, empty new Segment, token disappearance, posting with zero net DF change, deterministic order, repeated A->B->C->delete ownership, threshold crossings, cancellation, and exact equality with a clean base.

- [ ] **Step 2: Freeze replacement and delta contracts**

A delta lives at `objects/search/deltas/<delta_id>/`. Its manifest binds parent View, target Generation and manifest SHA-256, Search View recipe, scalar `statistics_delta`, all Task 4 layer receipts, and one sorted unique `replacements` array:

```text
{doc_key, doc_uid, old_segment_hash|null,
 old_summary_sha256|null, old_summary_bytes|null,
 new_segment_hash|null, new_summary_sha256|null,
 new_summary_bytes|null, new_doc_ordinal|null}
```

Only add `(null,new)`, edit `(old,new; old != new)`, and delete `(old,null)` are legal. Old hash and old summary receipt must equal the parent owner; new hash must equal the target Generation and its new summary receipt must match the just-projected Segment; new non-null records occur exactly once in the delta document table; deletes have no new receipt, ordinal, PCV, or posting rows. `delta_id = canonical_hash(delta_core)`.

- [ ] **Step 3: Build only changed data and signed touched-token contributions**

The posting/PCV layer contains only new versions of added/changed documents. Removed/old postings are never copied and there are no per-token tombstones. Load old/new summary sidecars only under the receipts bound by the parent owner/new replacement, then emit signed `df_any/df_nonbody/df_body` records for the touched-token union, including negative-only disappearance records. Use sparse lookups of those same tokens in the parent layers to verify non-negative effective after-stats and compute `token_count_delta`; never scan the base vocabulary or open base postings.

- [ ] **Step 4: Patch control-plane View artifacts only**

Apply scalar deltas to parent `statistics.json`; apply replacements to the document owner map; append the delta ID in chronological order; and write/finalize the new View. Owner records bind layer kind/ID/ordinal and segment hash. `CompactionPolicy` computes a recommendation in the build result, but that recommendation/policy is excluded from immutable object bytes and IDs and never triggers compaction here.

- [ ] **Step 5: Add proportional-work counters**

Report changed old/new summaries and Segments loaded, projected postings, touched tokens, parent term windows read, base posting bytes read, bytes written, layer count, and peak live Segments. For one edit, `segments_loaded_peak <= 1`, `base_posting_bytes_read == 0`, and postings/term work is bounded by the changed document.

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/pageindex_v3/test_delta_builder.py -q`
Expected: every incremental View has the same logical Generation, owners, scalar/token statistics, raw effective postings, and chunk metrics as a clean base.

- [ ] **Step 7: Commit**

```powershell
git add app/index/v3/delta_builder.py tests/pageindex_v3/test_delta_builder.py
git commit -m "feat(pageindex): build document replacement deltas"
```

### Task 8: Add independent bounded Normal validation for P3 objects

**Files:**
- Create: `app/index/v3/validator.py`
- Create: `tests/pageindex_v3/test_validator.py`

**Interfaces:**
- Consumes: logical Generation/Base/Delta/View receipts, Task 4 readers, Task 3 summaries, and exact parent/target manifests.
- Produces: `validate_generation_normal()`, `validate_base_normal()`, `validate_delta_normal()`, and `validate_view_normal()` using the existing `ValidationReport` shape.

- [ ] **Step 1: Write malicious re-bound receipt tests**

Rewrite artifacts and consistently rebind receipt/manifest hashes. Normal must still reject wrong artifact kind/schema/identity, unsafe or symlink paths, noncanonical files, sparse-term/PIV/PCV offset mismatches, invalid ordinals/local IDs, postings outside new replacements, malformed add/edit/delete states, wrong parent/target hash, token/scalar arithmetic errors, owner-map drift, delta reordering, and cyclic chains.

- [ ] **Step 2: Prove dirty Normal never scans base postings or vocabulary**

Monkeypatch base `postings.piv` open/read, full `terms.jsonl` iteration, and both v2 compilers to raise. A correct one-document delta must validate using only target Generation, parent/new small manifests, parent owner map/scalars, changed summaries, touched-token sparse windows, and the new delta/View artifacts.

- [ ] **Step 3: Implement validation by trust boundary**

Generation validation independently derives logical identity and binds exact proof/ref attestations. Full-base validation streams its own document table, PIV, PCV, term records, sparse index, and scalar totals once.

Dirty delta validation independently checks:

1. exact files, receipts, parent/target/recipe identity and ordered acyclic chain;
2. replacement states and agreement with parent owner map/target Generation;
3. PIV/PCV rows reference exactly live new replacements;
4. every old/new summary file matches the trusted owner/replacement SHA-256 and size before parsing, and `statistics_delta == sum(new_summary - old_summary)`;
5. term deltas equal the authenticated changed-summary union;
6. for each touched token, parent effective triple plus delta is non-negative, each DF is `<= total_chunks_after`, and `max(df_nonbody,df_body) <= df_any <= df_nonbody+df_body`;
7. new scalar totals and token_count equal parent plus delta;
8. new owner map equals parent owners plus replacements and every owner segment equals the target Generation.

It does not trust a re-bound receipt as semantic evidence and does not load unrelated Segments.

- [ ] **Step 4: Run focused validation tests**

Run: `python -m pytest tests/pageindex_v3/test_validator.py -q`
Expected: deterministic error ordering, bounded touched-token reads, zero base-posting reads on dirty Normal, and independent full-base verification only when a base is explicitly created.

- [ ] **Step 5: Commit**

```powershell
git add app/index/v3/validator.py tests/pageindex_v3/test_validator.py
git commit -m "feat(pageindex): validate base and delta views independently"
```

### Task 9: Implement a generation/view-pinned owner-map reader

**Files:**
- Create: `app/index/v3/reader.py`
- Create: `tests/pageindex_v3/test_reader.py`

**Interfaces:**
- Consumes: explicit `ViewPin(generation, view_id)`, Generation/View manifests, owner map, layer readers, PCV metrics, and Segments.
- Produces: `PinnedSearchView.open(...)`, `iter_raw_postings(token)`, `iter_effective_postings(token)`, `token_stats(tokens)`, `get_chunk_metrics(refs)`, `get_chunks(refs)`, `corpus_stats()`, and `documents()`.

- [ ] **Step 1: Write owner-map/newest-wins oracle tests**

Cover edit with an old token disappearing, add, delete without new rows, multiple replacements A->B->C->delete, same slug/different type, missing parent, explicit stale/mismatched pin, and a concurrently changed external pointer that cannot affect an already-open reader. Compare every result with a clean base.

- [ ] **Step 2: Open one immutable pin and construct ownership independent of tokens**

`open()` requires complete 64-hex Generation and View IDs, verifies the View-bound Generation manifest digest, exact ordered delta chain, owner map, and recipes, and then keeps that immutable state. It never reads `current.json`, checks mtimes, or chooses a latest fallback.

For every decoded layer row, acceptance is solely:

```python
owner = self.owner_by_doc_uid.get(layer_document.doc_uid)
accept = (
    owner is not None
    and owner.owner_layer_id == layer.id
    and owner.doc_ordinal == row.doc_ordinal
    and owner.segment_hash == layer_document.segment_hash
)
```

Token presence is never used to determine ownership, so disappearance/deletion cannot leak stale rows.

- [ ] **Step 3: Resolve token statistics and field policy on demand**

For each requested token, sparse-seek its base contribution and every ordered delta contribution, sum `df_any/df_nonbody/df_body`, and validate the pinned result. Compute:

```python
prune_body = (
    df_body >= recipe.body_df_min
    and df_body * recipe.body_df_ratio_denominator
        >= total_chunks * recipe.body_df_ratio_numerator
)
effective_df = df_nonbody if prune_body else df_any
```

Read non-body partitions from newest delta to base. Read body partitions only when not pruned, merge fields by complete `ChunkRef`, apply the owner predicate, drop all-zero rows, and return deterministic `(doc_uid, segment_hash, local_id)` order. Title/breadcrumb TF are never suppressed.

- [ ] **Step 4: Load only candidate metrics and chunks**

Group candidate refs by owner layer/document to seek only required PCV blocks. After ranking, group stable refs by `(doc_uid, segment_hash)`, call `SegmentProjector.load_chunks()` for requested local IDs, and use a byte-bounded thread-safe LRU keyed by segment hash. Never materialize all View chunks/postings/terms.

- [ ] **Step 5: Run reader tests and read-amplification assertions**

Run: `python -m pytest tests/pageindex_v3/test_reader.py -q`
Expected: incremental and clean-base raw/effective postings, token triples, metrics, and chunks match exactly; one-token reads are bounded by one sparse window and at most one addressed partition per layer.

- [ ] **Step 6: Commit**

```powershell
git add app/index/v3/reader.py tests/pageindex_v3/test_reader.py
git commit -m "feat(pageindex): read pinned base and delta views"
```

### Task 10: Add a P3 scorer and shadow query-equivalence adapter

**Files:**
- Create: `app/retrieval/search_view.py`
- Create: `tests/pageindex_v3/test_search_view.py`
- Modify: `app/http/schemas.py`
- Modify: `app/http/routes_search.py`
- Add: `tests/http/test_search_view_shadow.py`

**Interfaces:**
- Consumes: `PinnedSearchView`, existing tokenizer/query expansion/RRF/`Hit` semantics.
- Produces: `search_pinned_view(query, view, top_k) -> list[Hit]` and an opt-in `/api/search` shadow comparison that does not change the response source.

- [ ] **Step 1: Lock BM25 and multi-path parity on a query corpus**

Use queries covering body, title phrase, breadcrumb, document title routing, repeated terms, CJK, English, high-DF body pruning, and empty results. Compare stable result identities and scores against the legacy `search_multi_path` oracle with the same Segment facts.

- [ ] **Step 2: Implement candidate-only BM25F**

Collect effective raw field postings only for expanded query tokens, use each token's pinned `effective_df`, plus `total_chunks` and field length sums for IDF/normalization, load only candidate chunks, then reuse existing phrase scoring and RRF ordering. Do not call `build_chunk_stats()`, build a global CID map, or read root `inverted-index.json`/`chunks.json`.

- [ ] **Step 3: Add stable response references without breaking clients**

Add optional response fields `generation`, `view_id`, `doc_key`, `doc_uid`, `segment_hash`, `local_id`, and `node_key`; retain existing `doc_type`, `slug`, `node_id`, `title`, `breadcrumb`, `text`, and `score`.

- [ ] **Step 4: Add an opt-in shadow comparison path**

When `request.app.state.search_view_shadow_pin` is set, open that exact pin, run P3 off the event loop using FastAPI's synchronous handler/threadpool boundary, compare top results/latency, and record diagnostics without serving P3 results. One request holds one pin throughout.

- [ ] **Step 5: Run retrieval and HTTP tests**

Run: `python -m pytest tests/pageindex_v3/test_search_view.py tests/http/test_search_view_shadow.py tests/retrieval -q`
Expected: legacy response compatibility passes; P3 shadow result differences are zero on the locked corpus.

- [ ] **Step 6: Commit**

```powershell
git add app/retrieval/search_view.py app/http/schemas.py app/http/routes_search.py tests/pageindex_v3/test_search_view.py tests/http/test_search_view_shadow.py
git commit -m "feat(search): add pinned search view shadow reader"
```

### Task 11: Add the P3 worker, explicit optimize, and optional legacy export

**Files:**
- Create: `app/index/v3/protocol.py`
- Create: `app/index/v3/worker.py`
- Create: `app/index/v3/supervisor.py`
- Create: `app/index/v3/legacy_export.py`
- Create: `app/pageindex_v3_worker.py`
- Create: `tests/pageindex_v3/test_worker_protocol.py`
- Create: `tests/pageindex_v3/test_legacy_export.py`
- Modify: `app/index/v2/supervisor.py`
- Modify: `tests/pageindex_v2/test_worker_protocol.py`

**Interfaces:**
- Consumes: all previous P3 components and existing v2 Segment building/object store.
- Produces: protocol-v1 P3 request/result with `{generation, view_id}`, modes `incremental` and `optimize`, `legacy_export` in `{"none","full"}`, and fresh-process supervisor functions.

- [ ] **Step 1: Write strict protocol and lifecycle tests**

An incremental request accepts an optional base pair; no pair bootstraps a full base, while a pair produces no-op or one delta. `optimize` requires a pair and creates a new base View for the same Generation. Results attest both manifests. Mixed/missing pairs, unsafe IDs, stale parent pairs, malformed legacy-export flags, and protocol-v2/v1 confusion fail.

- [ ] **Step 2: Implement the default incremental pipeline**

```text
stable catalog snapshot
  -> no-op proof check
  -> SegmentChangeSet
  -> build only dirty Segments + summary sidecars
  -> logical Generation manifest
  -> one document-replacement delta
  -> P3 Normal validation
  -> immutable Generation/Delta/View finalize
```

The dirty path sets `legacy_compile_runs=0`, never calls either v2 compiler, never writes schema-3 monoliths, and returns `ready_to_publish` only after receipts validate. Instrument source-hash time, dirty Segment time, Generation time, delta time, Normal time, Segment loads/peak, postings visited, and bytes written.

- [ ] **Step 3: Implement explicit optimize**

`mode="optimize"` streams all current refs into a new base and View under the same logical Generation. An incremental build only emits `compaction_recommended`; it never invokes optimize automatically.

- [ ] **Step 4: Isolate legacy full export**

`legacy_export="full"` explicitly wraps the existing P2 streaming compatibility compiler and Normal validator, writes to `exports/legacy/<logical_generation>/<export_id>/`, and reports its own counters. `legacy_export="none"` is the default. Exact bytes must match the existing schema-3 oracle.

- [ ] **Step 5: Keep v2 shadow discovery from selecting P3 manifests**

Update v2 latest-generation scanning to accept only old schema-2/3 compatibility manifests (missing `artifact_kind` or explicit legacy kind). P3 scans only `artifact_kind="logical_generation"`; neither silently treats the other as a base.

- [ ] **Step 6: Run worker/legacy tests**

Run: `python -m pytest tests/pageindex_v3/test_worker_protocol.py tests/pageindex_v3/test_legacy_export.py tests/pageindex_v2/test_worker_protocol.py -q`
Expected: v2 behavior stays byte-compatible; P3 edit/delete loads only changed old/new Segments and has zero legacy compiles unless explicitly requested.

- [ ] **Step 7: Commit**

```powershell
git add app/index/v3/protocol.py app/index/v3/worker.py app/index/v3/supervisor.py app/index/v3/legacy_export.py app/pageindex_v3_worker.py app/index/v2/supervisor.py tests/pageindex_v3/test_worker_protocol.py tests/pageindex_v3/test_legacy_export.py tests/pageindex_v2/test_worker_protocol.py
git commit -m "feat(pageindex): build incremental search views by default"
```

### Task 12: Prove exact-50k dirty gates and document the P4 cutover seam

**Files:**
- Create: `app/index/v3/benchmark.py`
- Create: `tests/pageindex_v3/test_benchmark.py`
- Create: `docs/pageindex-v3-p3-performance-evidence.md`
- Modify: `docs/pageindex-v3-deep-incremental-design.md`

**Interfaces:**
- Consumes: P3 supervisor/worker, P2 exact synthetic corpus/process monitor, pinned reader/search adapter.
- Produces: fresh-process cold/no-op/edit/delete/optimize/query reports with mechanism assertions and a reproducible P3 evidence document.

- [ ] **Step 1: Add isolated benchmark scenarios**

Reuse the exact-50k corpus generator but keep a separate P3 PageIndex root. Every sample launches a fresh worker. Run one bootstrap base, 20 no-op samples, at least 20 deterministic one-document edits, at least 20 distinct one-document deletes, one explicit optimize, and a fixed query set against pinned clean-base and incremental Views.

- [ ] **Step 2: Enforce result and mechanism gates in the harness**

For dirty/delete require: `legacy_compile_runs=0`, `segments_loaded_peak<=1`, loaded Segment count bounded by old+new changed documents, `postings_visited` bounded by changed-document postings, no base posting scan, and View query results equal a clean base. Require no-op zero loads/visits/writes. Require all process metrics measured.

- [ ] **Step 3: Run focused and full test suites**

```powershell
python -m pytest tests/pageindex_v3 -q
python -m pytest tests/pageindex_v2 -q
python -m pytest -q
```

Expected: all tests pass; no compatibility fixture changes outside explicitly additive response fields.

- [ ] **Step 4: Run exact-50k performance evidence**

```powershell
python -m app.index.v3.benchmark `
  --content E:\pageindex-v3-p3-exact50k-20260802\content `
  --pageindex E:\pageindex-v3-p3-exact50k-20260802\pageindex `
  --synthetic-profile exact-50k `
  --bootstrap-runs 1 --noop-runs 20 `
  --edit-runs 20 --delete-runs 20 --optimize-runs 1 `
  --require-os-metrics `
  --output E:\pageindex-v3-p3-exact50k-20260802\report.json
```

Require edit/delete P95 `<5,000 ms`, no-op P95 `<500 ms`, Peak Working Set/Private Bytes `<536,870,912`, and pinned query P95 regression `<10%`.

- [ ] **Step 5: Record raw hashes, tradeoffs, and P4 boundary**

Document branch/commit, OS/Python, report paths and SHA-256, corpus hash, wall/peak/I/O distributions, disk cost per base/delta/summary, layer depth, query parity, and failures. State explicitly that `current.json` publisher, rollback leases, chat async migration, and delayed GC remain disabled until the separate P4 cutover plan passes its concurrency tests.

- [ ] **Step 6: Commit**

```powershell
git add app/index/v3/benchmark.py tests/pageindex_v3/test_benchmark.py docs/pageindex-v3-p3-performance-evidence.md docs/pageindex-v3-deep-incremental-design.md
git commit -m "test(pageindex): prove deep incremental search views"
```

## Completion Review

- [ ] Verify P3 incremental no-op returns before Segment loading and dirty builds never call either v2 compatibility compiler.
- [ ] Verify logical Generation IDs are independent of View layout, compaction, task IDs, paths, timestamps, and legacy export settings.
- [ ] Verify every posting layer retains raw title/breadcrumb/body TF and body policy reverses correctly across both threshold directions.
- [ ] Verify token disappearance and deletion suppress older rows solely through document replacement semantics.
- [ ] Verify a pinned reader never rereads current, never chooses latest mtime, and never parses the full posting layer for one token.
- [ ] Verify Normal dirty validation touches only new artifacts, small parent attestations, and changed old/new summaries.
- [ ] Verify explicit optimize changes `view_id` but not `generation`; legacy export is byte-identical and opt-in only.
- [ ] Verify exact-50k reports prove result equivalence, mechanism proportionality, resource gates, and query latency rather than inferring them from unit tests.
- [ ] Verify no P3 code writes `current.json`; formal publication remains gated on the P4 CAS/pinning/rollback plan.
