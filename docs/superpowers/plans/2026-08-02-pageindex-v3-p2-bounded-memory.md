# PageIndex v3 P2 Bounded-Memory Compiler Implementation Plan

## Execution Status (2026-08-02)

Completed and verified on branch `codex/pageindex-v3-deep-incremental`. The full suite passed with 476 tests and 2 skips; exact-50k cold peak working set fell from 6,280,855,552 B to 198,983,680 B, no-op P95 was 497.927 ms, and explicit Deep Audit passed in a separate process. Dirty edit/delete still spend about 129-140 seconds in the schema-3 compatibility recompile, so that remaining gate is intentionally handed to the P3 base+delta plan. Raw commands, report hashes, tradeoffs, and mechanism counters are recorded in `docs/pageindex-v3-p2-performance-evidence.md`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the production PageIndex compatibility build with a bounded-memory, reference-driven compiler and a Normal validator that never performs a second full build, while preserving byte-for-byte schema-3 output and existing publish safety.

**Architecture:** The worker owns only immutable `StoredSegmentRef` values. A streaming compiler loads one Segment at a time, writes canonical JSON artifacts through hashing sinks, spills postings into bounded sorted runs, and merges those runs directly into `inverted-index.json`. The compiler returns a lightweight `CandidateReceipt`; Normal validation checks paths, lineage, hashes, sizes, ordering, references, and aggregate conservation without loading all payloads or calling the legacy compiler. The existing in-memory compiler remains the Deep/golden oracle and is never used on the normal build path.

**Tech Stack:** Python 3.10+, standard library (`dataclasses`, `hashlib`, `heapq`, `json`, `os`, `pathlib`, `struct`, `tempfile`), pytest, existing PageIndex v2 schemas and benchmark harness.

## Global Constraints

- Preserve the exact schema-3 generation bytes for identical Segment inputs and `CompilerRecipe`.
- Keep title and breadcrumb postings unconditionally; prune only extreme-DF body contributions using the existing recipe thresholds.
- At most one decoded Segment may be live in the compiler at a time.
- Posting run buffers are limited by encoded bytes, not row count; merge fan-in is bounded and configurable.
- Normal validation must not call `compile_generation()`, load every Segment, tokenize reused Segments, or parse an entire large runtime artifact.
- Deep validation remains explicit/CI/manual and must run in a short-lived process before it is enabled operationally.
- Candidate failures never modify a published Generation or `current.json`.
- P2 optimizes memory and removes duplicate full work. It does not claim the dirty-build `<5s` gate; that requires the P3 base+delta Search View.
- P2 interfaces must be reusable by P3: immutable Segment refs, artifact receipts, projection iterators, and `ValidationMode` must not depend on global numeric chunk IDs.

---

### Task 1: Lock the old compiler as the byte-level oracle

**Files:**
- Add: `tests/pageindex_v2/test_streaming_compiler.py`
- Modify: `tests/pageindex_v2/conftest.py`

- [ ] Add a reusable fixture that builds at least three documents, Unicode text, empty optional fields, multiple nodes/chunks, a title-only token, breadcrumb-only token, body-only token, and an extreme-DF body token.
- [ ] Materialize `compile_generation()` output into an oracle directory and record every relative path and raw byte string.
- [ ] Add a test helper that compares candidate directories by exact file set and exact bytes, including `manifest.json`, `input-proof.json`, document trees, and `inverted-index.json`.
- [ ] Run `python -m pytest tests/pageindex_v2/test_compiler.py tests/pageindex_v2/test_streaming_compiler.py -q` and confirm the new test fails only because the streaming entry point does not exist.
- [ ] Commit with message `test(pageindex): lock compatibility generation bytes`.

### Task 2: Add immutable Segment references without changing object bytes

**Files:**
- Modify: `app/index/v2/object_store.py`
- Add: `tests/pageindex_v2/test_segment_refs.py`
- Modify: `tests/pageindex_v2/test_object_store.py`

- [ ] Introduce `StoredSegmentRef` as a frozen, slotted dataclass with `segment_hash`, `path`, `byte_size`, `doc_key`, `doc_type`, `slug`, `content_hash`, and `segment_recipe_hash`.
- [ ] Make `put_segment()` return `StoredSegmentRef`, deriving metadata from the one Segment already in memory; keep the current `hash` and `sha256` compatibility properties.
- [ ] Add `segment_ref_from_attestation(pageindex_dir, doc_key, segment_hash, content_hash, segment_recipe_hash)` that validates hashes/path and derives `doc_type` and `slug` from `doc_key` without decoding the object.
- [ ] Let `load_segment()` accept either a digest or `StoredSegmentRef`, verify the file digest, decode exactly one object, and preserve all corruption errors.
- [ ] Test valid refs, malformed `doc_key`, proof/hash mismatch, traversal rejection, and unchanged canonical object bytes.
- [ ] Run `python -m pytest tests/pageindex_v2/test_object_store.py tests/pageindex_v2/test_segment_refs.py -q`.
- [ ] Commit with message `refactor(pageindex): pass immutable segment references`.

### Task 3: Add one-pass canonical artifact sinks and receipts

**Files:**
- Add: `app/index/v2/artifacts.py`
- Modify: `app/index/v2/canonical.py`
- Add: `tests/pageindex_v2/test_artifacts.py`

- [ ] Add frozen, slotted `ArtifactRef(relative_path, sha256, byte_size, records)` and `CandidateReceipt(candidate_dir, generation_id, revision_sha256, compiler_recipe_hash, input_proof_sha256, manifest_sha256, artifacts, segment_refs, invariants)` dataclasses.
- [ ] Implement an atomic hashing sink that writes to a sibling temporary file, updates SHA-256 and byte count on every write, flushes/fsyncs, closes, then replaces the destination.
- [ ] Implement canonical object, object-with-array, and object-with-mapping writers using `json.JSONEncoder(...).iterencode()` so no complete encoded payload is created.
- [ ] Guarantee lexicographic object-key order and exact `canonical_bytes()` equivalence for ASCII, Unicode, escaped strings, integers, floats used by recipes, nested objects, and empty containers.
- [ ] On an injected write exception, close and remove the temporary file and leave any old destination untouched.
- [ ] Run `python -m pytest tests/pageindex_v2/test_artifacts.py -q`.
- [ ] Commit with message `feat(pageindex): stream canonical artifacts with receipts`.

### Task 4: Implement bounded posting runs and fan-in merge

**Files:**
- Add: `app/index/v2/posting_runs.py`
- Add: `tests/pageindex_v2/test_posting_runs.py`

- [ ] Define `PostingRecord(token, chunk_id, title_tf, breadcrumb_tf, body_tf)` and a length-prefixed binary run format whose reader rejects truncation, invalid UTF-8, negative values, zero total TF, duplicate `(token, chunk_id)`, and non-monotonic order.
- [ ] Implement `PostingRunBuilder(max_run_bytes)` that accounts for the encoded record size before accepting it, sorts and flushes before the bound is exceeded, and reports `run_buffer_peak_bytes`.
- [ ] Implement `merge_runs(paths, destination, fan_in)` using `heapq`, opening no more than `fan_in` inputs plus one output at once. Repeat passes until one sorted run remains.
- [ ] Ensure cancellation/exception cleanup closes all Windows handles before deleting intermediate runs.
- [ ] Test forced one-record runs, Unicode tokens, multiple merge levels, deterministic bytes under shuffled Segment order, duplicate detection, corrupt/truncated input, and observed open-file bound.
- [ ] Run `python -m pytest tests/pageindex_v2/test_posting_runs.py -q`.
- [ ] Commit with message `feat(pageindex): add bounded external posting merge`.

### Task 5: Stream schema-3 artifacts directly from Segment refs

**Files:**
- Modify: `app/index/v2/compiler.py`
- Modify: `app/index/v2/artifacts.py`
- Modify: `app/index/v2/posting_runs.py`
- Modify: `tests/pageindex_v2/test_streaming_compiler.py`

- [ ] Add `compile_generation_to_candidate(refs, pageindex_dir, candidate_dir, recipe, *, max_run_bytes=32*1024*1024, merge_fan_in=32) -> CandidateReceipt` while retaining `compile_generation()` only as Deep/golden code.
- [ ] Sort refs by the existing document order without decoding them, reject duplicate `doc_key`, and derive input proof/core manifest/revision/Generation ID from ref attestations.
- [ ] For each ref, load and validate one Segment in a local scope; confirm all ref metadata against the decoded Segment before emitting anything from it.
- [ ] Stream global documents, nodes, chunks, and each document tree in the same order and exact shape as the oracle. Keep only the current Segment's node/local-ID maps.
- [ ] Translate local posting IDs to compatibility global IDs and append records to the bounded run builder. Release the Segment and all per-document maps before loading the next ref.
- [ ] Merge posting runs. Scan each token group once for `body_df`, then stream its rows into canonical `inverted-index.json`; use seekable run offsets/two-pass group reads so one extremely frequent token cannot become an unbounded list.
- [ ] Compute exact `tokens/postings/body_*_pruned/estimated_bytes_saved` counters from emitted bytes and group statistics without constructing `normalized_postings`, `unpruned_export`, or `exported_postings`.
- [ ] Write `input-proof.json` and `manifest.json` last; populate `files` from `ArtifactRef` values and return a lightweight `CandidateReceipt` containing writer invariants and peak-buffer instrumentation.
- [ ] Assert the new candidate is byte-for-byte identical to the old compiler across the rich fixture, reversed refs, forced multi-level merges, zero documents, and threshold boundary cases.
- [ ] Add lifecycle instrumentation and assert `segments_loaded_peak <= 1` and `run_buffer_peak_bytes <= max_run_bytes + largest_record_bytes`.
- [ ] Run `python -m pytest tests/pageindex_v2/test_compiler.py tests/pageindex_v2/test_streaming_compiler.py tests/pageindex_v2/test_posting_runs.py -q`.
- [ ] Commit with message `feat(pageindex): compile compatibility index with bounded memory`.

### Task 6: Make the worker reference-only after discovery

**Files:**
- Modify: `app/index/v2/worker.py`
- Modify: `app/index/v2/input_proof.py`
- Modify: `tests/pageindex_v2/test_worker_protocol.py`
- Modify: `tests/pageindex_v2/test_source_snapshot.py`

- [ ] Replace `_read_base_segments()` with `_read_base_segment_refs()` that reads manifest and input proof attestations, verifies their document sets, and never calls `load_segment()`.
- [ ] Replace `_base_reusable_segments()` and `_source_segments()` with ref-based equivalents. Reused sources append refs only; rebuilt Segment dicts are persisted, converted to refs, then released immediately.
- [ ] Keep bootstrap object-store reuse behavior isolated to the no-lineage first build; record that scan cost separately and never use it when a base Generation is supplied.
- [ ] Change `_compile_and_validate()` to call `compile_generation_to_candidate()` and pass/return `CandidateReceipt`, not `CompiledGeneration.payloads`.
- [ ] Preserve protocol fields while adding instrumentation: `segments_loaded_peak`, `run_buffer_peak_bytes`, `generation_bytes_written`, `full_compile_runs`, `normal_validation_runs`, and `deep_validation_runs`.
- [ ] Test that dirty and recompile paths pass only `StoredSegmentRef`; monkeypatch pre-compiler `load_segment()` to fail for reused refs; verify rebuilt/reused/deleted counts and source-stability retries.
- [ ] Run `python -m pytest tests/pageindex_v2/test_worker_protocol.py tests/pageindex_v2/test_source_snapshot.py tests/pageindex_v2/test_incremental_equivalence.py -q`.
- [ ] Commit with message `refactor(pageindex): keep worker segment ownership bounded`.

### Task 7: Split Normal validation from the legacy Deep oracle

**Files:**
- Modify: `app/index/v2/validator.py`
- Add: `app/index/v2/streaming_json.py`
- Modify: `app/index/v2/worker.py`
- Modify: `tests/pageindex_v2/test_validator.py`
- Add: `tests/pageindex_v2/test_validator_modes.py`

- [ ] Add `ValidationMode` with `NORMAL`, `SAMPLED`, and `DEEP`; expose `validate_candidate_normal(receipt, pageindex_dir)` and rename the current semantic recompilation path to `validate_candidate_deep(candidate_dir, pageindex_dir)`.
- [ ] Normal validates manifest schema/recipe/hash/revision/Generation ID, exact safe file set, proof/ref/document binding, and all artifact SHA-256/byte sizes with bounded streaming reads.
- [ ] Add bounded canonical readers for the large wrapper arrays and inverted mapping. Validate canonical syntax, monotonic token/chunk order, unique postings, positive TF, contiguous chunk IDs, node/document references, and manifest aggregate/pruning conservation without loading a full payload.
- [ ] Validate newly rebuilt Segment objects deeply while still in their one-object lifecycle; treat reused content-addressed Segment refs as attestations in Normal. Sampled/Deep covers bit rot and semantic recomputation of reused objects.
- [ ] Make the worker call only Normal. Monkeypatch both `compile_generation()` and full-set Segment loading to raise, and prove a valid normal build still succeeds.
- [ ] Move full posting recomputation, full runtime semantic comparison, `compiled_payload_mismatch`, stats/pruning exact recompile checks, and corrupt reused-object audits to Deep-mode tests.
- [ ] Keep all Normal integrity failures publish-blocking and stable: missing/extra/unsafe file, hash/size/canonical mismatch, proof/ref mismatch, aggregate mismatch, ordering violation, and reference violation.
- [ ] Run `python -m pytest tests/pageindex_v2/test_validator.py tests/pageindex_v2/test_validator_modes.py tests/pageindex_v2/test_worker_protocol.py -q`.
- [ ] Commit with message `refactor(pageindex): separate normal and deep validation`.

### Task 8: Finalize by receipts instead of directory byte materialization

**Files:**
- Modify: `app/index/v2/worker.py`
- Add: `tests/pageindex_v2/test_generation_finalize.py`

- [ ] Delete `_directory_files()` and compare an existing/concurrent Generation using canonical manifest digest plus its artifact `(path, sha256, bytes)` map.
- [ ] Stream-hash the existing `manifest.json`; do not call `Path.read_bytes()` on large artifacts. Reject a different manifest, missing artifact, size mismatch, or unsafe path before removing the candidate.
- [ ] Ensure all posting run handles and artifact sinks are closed before `os.replace()` on Windows.
- [ ] Test identical existing Generation, differing manifest, truncated existing artifact, concurrent identical finalize, and concurrent different finalize. Patch large-artifact `read_bytes()` to raise so the test proves bounded reads.
- [ ] Run `python -m pytest tests/pageindex_v2/test_generation_finalize.py tests/pageindex_v2/test_worker_protocol.py -q`.
- [ ] Commit with message `perf(pageindex): finalize generations from digests`.

### Task 9: Keep Deep audit out of the normal process lifecycle

**Files:**
- Add: `app/index/v2/audit_worker.py`
- Modify: `app/index/v2/validator.py`
- Add: `tests/pageindex_v2/test_audit_worker.py`

- [ ] Add a narrow request/result protocol for explicitly invoking `validate_candidate_deep()` in a new Python process after the build worker exits.
- [ ] Record `audit_error` separately from semantic validation failures and never report a process crash as index corruption.
- [ ] Do not enable periodic scheduling in this change; document supported triggers as CI, recipe/schema migration, manual audit, and future deterministic sampling escalation.
- [ ] Test distinct PID, deterministic error ordering, cancellation, corrupt reused Segment detection, semantic payload tampering, and no mutation of published/current state on failure.
- [ ] Run `python -m pytest tests/pageindex_v2/test_audit_worker.py tests/pageindex_v2/test_validator.py -q`.
- [ ] Commit with message `feat(pageindex): isolate deep generation audits`.

### Task 10: Add dirty/full performance scenarios and prove P2 gates

**Files:**
- Modify: `app/index/v2/benchmark.py`
- Modify: `tests/pageindex_v2/test_benchmark.py`
- Modify: `docs/pageindex-v3-p0-p1-performance-evidence.md`
- Add: `docs/pageindex-v3-p2-performance-evidence.md`

- [ ] Add exact-corpus scenarios for cold full, unchanged incremental, one-document edit, one-document delete, and recompile. Run each measured sample in a fresh process so retained allocator pages do not pollute later peaks.
- [ ] Record per-stage duration and Peak Working Set/Private Bytes plus `segments_loaded_peak`, `run_buffer_peak_bytes`, `postings_visited`, `generation_bytes_written`, and validation mode counters.
- [ ] Add mechanism assertions: `segments_loaded_peak <= 1` during compatibility compile, `run_buffer_peak_bytes` within configured bound, `full_compile_runs == 1` for a real P2 build, `normal_validation_runs == 1`, and `deep_validation_runs == 0`.
- [ ] Run the focused suite, then `python -m pytest tests/pageindex_v2 -q`, then `python -m pytest -q`.
- [ ] Generate a fresh exact-50k corpus and run cold full plus at least 20 dirty samples. Require full-build Peak Working Set and Private Bytes `<512 MiB`; report full/dirty latency honestly without applying the P3 `<5s` gate.
- [ ] Compare candidate directories against the oracle on the small corpus and run a sampled Deep audit of the 50k output before declaring equivalence.
- [ ] Document raw report paths, commands, environment, medians/P95, disk/run-space cost, and remaining P3 work.
- [ ] Commit with message `test(pageindex): prove bounded-memory P2 build`.

### Task 11: Prepare the P3 seam without changing search behavior

**Files:**
- Add: `app/index/v3/__init__.py`
- Add: `app/index/v3/models.py`
- Add: `app/index/v3/segment_projection.py`
- Add: `tests/pageindex_v3/test_models.py`
- Add: `tests/pageindex_v3/test_segment_projection.py`

- [ ] Define separate `GenerationRecipe` (logical search semantics), `SearchViewRecipe` (base/delta physical layout), and `LegacyExportRecipe` (schema-3 compatibility output) so physical formats do not contaminate future logical Generation IDs.
- [ ] Reuse/adapt P2 `StoredSegmentRef` and `ArtifactRef`; add stable `doc_uid = SHA256(doc_key)` and `PostingRecord(doc_uid, local_id, title_tf, breadcrumb_tf, body_tf)`.
- [ ] Add `SegmentProjector.iter_postings(ref)`, `summarize(ref)`, and targeted `load_chunks(ref, local_ids)`, each loading at most the requested one Segment and never assigning a global numeric chunk ID.
- [ ] Lock field semantics in tests: title/breadcrumb are never DF-pruned; body retains raw contributions so the query-time policy can cross thresholds in both directions.
- [ ] Do not route production reads/writes to v3 in P2. This task only prevents the P2 ownership/artifact APIs from forcing another rewrite.
- [ ] Run `python -m pytest tests/pageindex_v3 -q` and the full PageIndex suite.
- [ ] Commit with message `refactor(pageindex): expose p3 projection seams`.

## Completion Review

- [ ] Verify every production worker build after no-op uses refs, the streaming compiler, and Normal validation only.
- [ ] Verify no placeholder, `TODO`, disabled assertion, or silent fallback to `compile_generation()` exists on the normal path.
- [ ] Verify exact schema-3 bytes and Generation ID are unchanged for equivalent input.
- [ ] Verify errors are deterministic and a failed/cancelled build leaves the published Generation untouched.
- [ ] Verify tests cover Windows handle closure, bounded run bytes/fan-in, one-live-Segment ownership, and streaming validation of the largest files.
- [ ] Verify the exact-50k report demonstrates `<512 MiB` rather than inferring it from unit tests.
- [ ] Record that P2 removes the 5.85 GiB failure and duplicate full validation, while P3 base+delta remains necessary for dirty P95 `<5s`.
