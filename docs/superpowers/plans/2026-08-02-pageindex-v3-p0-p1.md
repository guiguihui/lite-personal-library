# PageIndex v3 P0-P1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the misleading in-process capacity benchmark with isolated OS-level worker measurements and add a Generation-bound no-op proof that returns before any Segment load, compile, materialization, validation, or Shadow comparison.

**Architecture:** P0 reuses the production worker command and result verification while sampling one short-lived process with standard-library OS APIs. P1 raises the compiler/Generation schema to 3, emits a deterministic `input-proof.json` bound into the Generation revision, and compares that proof before loading the base Generation's Segment objects.

**Tech Stack:** Python 3.10+, standard library (`subprocess`, `ctypes`, `/proc`, `hashlib`, `json`), existing PageIndex v2 Segment/compiler pipeline, pytest 8+, canonical JSON task protocol.

## Global Constraints

- Incremental remains the default build mode; full remains an explicit optional mode.
- Segment objects remain the sole index fact source.
- P0/P1 do not switch the production legacy read path and do not write `current.json`.
- `title` and `breadcrumb` postings are never removed by DF.
- `body` is suppressed only when `body_chunk_df >= 256` and `body_chunk_df / total_chunks >= 0.90`.
- No database, message queue, resident search service, `psutil`, or new runtime dependency is introduced.
- Official performance evidence runs each worker in a new process and does not enable `tracemalloc`.
- Unknown or unavailable metrics are represented as `null` plus an explicit status; zero never means “not measured”.
- A no-op may trust a previously validated immutable Generation, but it must validate the canonical manifest, bound input proof, recipes, Generation identity, and current source content.
- A schema-2 Generation without an input proof falls back to the existing build path.
- Existing untracked content in the original worktree must not be modified or staged.

---

## File Structure

Create:

```text
app/index/v2/process_metrics.py
app/index/v2/input_proof.py
tests/pageindex_v2/test_process_metrics.py
tests/pageindex_v2/test_input_proof.py
docs/pageindex-v3-deep-incremental-design.md
docs/superpowers/plans/2026-08-02-pageindex-v3-p0-p1.md
```

Modify:

```text
app/index/v2/models.py
app/index/v2/compiler.py
app/index/v2/validator.py
app/index/v2/worker.py
app/index/v2/supervisor.py
app/index/v2/protocol.py
app/index/v2/benchmark.py
tests/pageindex_v2/test_compiler.py
tests/pageindex_v2/test_validator.py
tests/pageindex_v2/test_worker_protocol.py
tests/pageindex_v2/test_benchmark.py
docs/pageindex-v2-incremental-design.md
```

Responsibilities:

- `process_metrics.py`: attach to one worker PID and return explicit OS working-set, private-memory and I/O measurements.
- `input_proof.py`: build and validate deterministic source/recipe proofs.
- `supervisor.py`: provide one public worker-result verifier shared by production and benchmark paths.
- `benchmark.py`: launch one real worker per round, sample it, enforce exact chunk counts and aggregate OS metrics.
- `compiler.py`: bind `input-proof.json` into schema-3 Generation identity and payload metadata.
- `validator.py`: independently verify proof canonicality, digest, document membership and recipe identity.
- `worker.py`: test no-change before `_read_base_segments()` and return a mechanism-verifiable result.
- `protocol.py`: define and validate `built` and `no_change` success outcomes.

### Task 1: Record the v3 Boundary

**Files:**

- Create: `docs/pageindex-v3-deep-incremental-design.md`
- Create: `docs/superpowers/plans/2026-08-02-pageindex-v3-p0-p1.md`
- Modify: `docs/pageindex-v2-incremental-design.md`

**Interfaces:**

- Produces: one v3 design authority for Generation/View identity, no-op trust, P0-P4 gates and legacy migration.
- Produces: one executable P0/P1 implementation plan.

- [ ] **Step 1: Add the v3 design and implementation plan**

The v3 design must state these exact identity formulas:

```text
generation_id = hash(semantic_recipe + sorted(doc_key -> segment_hash))
view_id       = hash(generation_id + physical_recipe + base/delta refs)
```

It must also state that P1's `input-proof.json` is deterministic and bound to the schema-3 Generation revision, while P3's history-dependent Search View never enters the logical Generation ID.

- [ ] **Step 2: Add a v2 status note**

Append a dated note to the v2 document:

```markdown
### 31.7 50k 性能结论与 v3 后续

阶段 A 的确定性与结构正确性成立，但默认增量仍全量编译和完整校验，未达到 50k 性能门槛。深层增量、分层校验和运行时 Search View 的后续设计已迁移到 `docs/pageindex-v3-deep-incremental-design.md`；v2 保留为 Shadow 正确性基线。
```

- [ ] **Step 3: Review documentation constraints**

Run:

```powershell
rg -n "T[B]D|TO[D]O|implement la[t]er|fill in deta[i]ls" docs/pageindex-v3-deep-incremental-design.md docs/superpowers/plans/2026-08-02-pageindex-v3-p0-p1.md
```

Expected: no matches.

- [ ] **Step 4: Commit**

```powershell
git add docs/pageindex-v3-deep-incremental-design.md docs/pageindex-v2-incremental-design.md docs/superpowers/plans/2026-08-02-pageindex-v3-p0-p1.md
git commit -m "docs(pageindex): define v3 deep incremental architecture"
```

### Task 2: Add OS Process Metrics

**Files:**

- Create: `app/index/v2/process_metrics.py`
- Create: `tests/pageindex_v2/test_process_metrics.py`

**Interfaces:**

- Produces: `OsProcessMetrics.as_dict() -> dict[str, object]`
- Produces: `ProcessMonitor.attach(pid: int, sample_interval_ms: int = 10) -> ProcessMonitor`
- Produces: `ProcessMonitor.sample() -> None`
- Produces: `ProcessMonitor.finish() -> OsProcessMetrics`
- Produces: `ProcessMonitor.close() -> None`

- [ ] **Step 1: Write failing lifecycle and aggregation tests**

```python
def test_monitor_reports_unknown_values_as_none(fake_backend) -> None:
    fake_backend.samples = []
    metrics = ProcessMonitor(fake_backend, sample_interval_ms=10).finish()
    assert metrics.status == "unavailable"
    assert metrics.peak_working_set_bytes is None
    assert metrics.peak_private_bytes_observed is None


def test_monitor_keeps_os_peak_and_observed_private_peak(fake_backend) -> None:
    fake_backend.samples = [
        {"peak_working_set": 40, "private": 20, "read": 10, "write": 5},
        {"peak_working_set": 90, "private": 70, "read": 30, "write": 25},
        {"peak_working_set": 90, "private": 50, "read": 40, "write": 35},
    ]
    monitor = ProcessMonitor(fake_backend, sample_interval_ms=10)
    for _ in fake_backend.samples:
        monitor.sample()
    metrics = monitor.finish()
    assert metrics.peak_working_set_bytes == 90
    assert metrics.peak_private_bytes_observed == 70
    assert metrics.io_read_transfer_bytes == 40
    assert metrics.io_write_transfer_bytes == 35
```

- [ ] **Step 2: Run tests and verify the module is missing**

Run:

```powershell
python -m pytest tests/pageindex_v2/test_process_metrics.py -q
```

Expected: collection fails because `app.index.v2.process_metrics` does not exist.

- [ ] **Step 3: Implement the portable result contract**

```python
@dataclass(frozen=True, slots=True)
class OsProcessMetrics:
    backend: str
    status: str
    scope: str
    sample_interval_ms: int
    samples: int
    peak_working_set_bytes: int | None
    peak_private_bytes_observed: int | None
    peak_pagefile_usage_bytes: int | None
    io_read_operations: int | None
    io_write_operations: int | None
    io_read_transfer_bytes: int | None
    io_write_transfer_bytes: int | None
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)
```

On Windows, use `OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ)`, `GetProcessMemoryInfo(PROCESS_MEMORY_COUNTERS_EX)`, `GetProcessIoCounters(IO_COUNTERS)` and `CloseHandle`. `PeakWorkingSetSize` is the OS peak; `PrivateUsage` is sampled and therefore named `peak_private_bytes_observed`. Use `ctypes.c_size_t` for size fields.

On Linux, read `VmHWM`, `VmRSS` from `/proc/<pid>/status` and transfer counters from `/proc/<pid>/io`. Other platforms produce `status="unsupported"`, `None` values and a warning.

- [ ] **Step 4: Run focused tests**

Run:

```powershell
python -m pytest tests/pageindex_v2/test_process_metrics.py -q
```

Expected: all tests pass; on Windows a real short subprocess smoke test reports positive working-set bytes.

- [ ] **Step 5: Commit**

```powershell
git add app/index/v2/process_metrics.py tests/pageindex_v2/test_process_metrics.py
git commit -m "test(pageindex): measure worker OS resources"
```

### Task 3: Move Capacity Rounds to Real Worker Processes

**Files:**

- Modify: `app/index/v2/supervisor.py`
- Modify: `app/index/v2/benchmark.py`
- Modify: `tests/pageindex_v2/test_worker_protocol.py`
- Modify: `tests/pageindex_v2/test_benchmark.py`

**Interfaces:**

- Produces: `verify_worker_completion(result, request, pageindex_dir, returncode) -> None`
- Consumes: `worker_command(request_path) -> list[str]`
- Consumes: `ProcessMonitor.attach(pid, sample_interval_ms)`
- Produces: benchmark report schema 2 with `process_metrics` for every round.

- [ ] **Step 1: Write failing shared-verifier tests**

```python
def test_verify_worker_completion_rejects_exit_status_disagreement(...):
    result = valid_success_result()
    with pytest.raises(WorkerProcessError, match="exit code"):
        verify_worker_completion(result, request, pageindex, returncode=1)
```

Move the existing status/exit-code checks from `run_shadow_build()` into the public verifier, followed by the existing `_verify_success_result()` checks.

- [ ] **Step 2: Write failing subprocess benchmark tests**

```python
def test_capacity_benchmark_uses_a_fresh_worker_for_each_round(...):
    report = run_capacity_benchmark(
        content,
        pageindex,
        full_runs=1,
        incremental_runs=2,
        synthetic=tiny_spec,
    )
    assert report["schema_version"] == 2
    assert len({round_["worker_pid"] for round_ in report["rounds"]}) == 3
    assert all(round_["process_metrics"]["samples"] > 0 for round_ in report["rounds"])
```

- [ ] **Step 3: Verify new tests fail against the in-process runner**

Run:

```powershell
python -m pytest tests/pageindex_v2/test_benchmark.py tests/pageindex_v2/test_worker_protocol.py -q
```

Expected: failures for missing schema 2, worker PIDs, process metrics and public verifier.

- [ ] **Step 4: Implement the monitored subprocess round**

Use a command argument array and job-local binary log files:

```python
process = subprocess.Popen(
    worker_command(request_path),
    cwd=project_root,
    stdout=stdout_stream,
    stderr=stderr_stream,
    shell=False,
)
monitor = ProcessMonitor.attach(
    process.pid,
    sample_interval_ms=sample_interval_ms,
)
while process.poll() is None:
    monitor.sample()
    time.sleep(sample_interval_ms / 1000)
monitor.sample()
metrics = monitor.finish()
returncode = process.wait()
```

Always close the monitor and log streams. If interrupted, terminate only the exact child PID, wait for it, and keep the legacy-file integrity check. After reading `result.json`, call `verify_worker_completion()`.

Remove `gc.collect()`, official in-process `tracemalloc` reporting and direct `run_worker()` calls from the default benchmark path.

- [ ] **Step 5: Aggregate OS metrics**

For `wall_time_ms`, `peak_working_set_bytes`, `peak_private_bytes_observed`, `io_read_transfer_bytes` and `io_write_transfer_bytes`, report `min`, `median`, nearest-rank `p95` and `max` over non-null values. If every value is null, return null for that metric.

- [ ] **Step 6: Run focused tests**

Run:

```powershell
python -m pytest tests/pageindex_v2/test_process_metrics.py tests/pageindex_v2/test_benchmark.py tests/pageindex_v2/test_worker_protocol.py -q
```

Expected: all tests pass and real tiny rounds use distinct PIDs.

- [ ] **Step 7: Commit**

```powershell
git add app/index/v2/supervisor.py app/index/v2/benchmark.py tests/pageindex_v2/test_benchmark.py tests/pageindex_v2/test_worker_protocol.py
git commit -m "test(pageindex): isolate capacity benchmark workers"
```

### Task 4: Enforce Exact 50k Capacity Input

**Files:**

- Modify: `app/index/v2/benchmark.py`
- Modify: `tests/pageindex_v2/test_benchmark.py`

**Interfaces:**

- Produces: `SyntheticCorpusSpec.exact_50k() -> SyntheticCorpusSpec`
- Produces: `expected_chunks: int | None` in the corpus spec/report.
- Produces: CLI `--synthetic-profile exact-50k` and `--require-os-metrics`.

- [ ] **Step 1: Write failing profile tests**

```python
def test_exact_50k_profile_has_the_measured_fixture_shape() -> None:
    spec = SyntheticCorpusSpec.exact_50k()
    assert spec.documents == 1000
    assert spec.sections_per_document == 50
    assert spec.words_per_section == 48
    assert spec.vocabulary_size == 4096
    assert spec.seed == 42
    assert spec.expected_chunks == 50000
```

Also add a tiny real build with `expected_chunks=6` and assert the report records `observed_chunks=6` and `exact_chunk_count=True`.

- [ ] **Step 2: Verify the profile tests fail**

Run:

```powershell
python -m pytest tests/pageindex_v2/test_benchmark.py -q
```

Expected: failure because the profile and expected chunk gate do not exist.

- [ ] **Step 3: Implement the profile and hard postcondition**

```python
@classmethod
def exact_50k(cls) -> "SyntheticCorpusSpec":
    return cls(
        documents=1000,
        sections_per_document=50,
        words_per_section=48,
        vocabulary_size=4096,
        seed=42,
        profile="exact-50k",
        expected_chunks=50000,
    )
```

After every round:

```python
observed = worker_stats.get("chunks")
if synthetic.expected_chunks is not None and observed != synthetic.expected_chunks:
    raise BenchmarkError(
        f"{synthetic.profile} expected {synthetic.expected_chunks} chunks, "
        f"observed {observed}"
    )
```

Increment the synthetic marker schema so an older 128-word corpus cannot be silently reused as exact-50k.

- [ ] **Step 4: Run focused tests and CLI smoke**

Run:

```powershell
python -m pytest tests/pageindex_v2/test_benchmark.py -q
python -m app.index.v2.benchmark --help
```

Expected: tests pass and help lists both new flags.

- [ ] **Step 5: Commit**

```powershell
git add app/index/v2/benchmark.py tests/pageindex_v2/test_benchmark.py
git commit -m "test(pageindex): define exact 50k capacity profile"
```

### Task 5: Bind an Input Proof into Generation Schema 3

**Files:**

- Create: `app/index/v2/input_proof.py`
- Create: `tests/pageindex_v2/test_input_proof.py`
- Modify: `app/index/v2/models.py`
- Modify: `app/index/v2/compiler.py`
- Modify: `tests/pageindex_v2/test_compiler.py`

**Interfaces:**

- Produces: `proof_from_segments(segments, compiler_recipe_hash) -> dict[str, object]`
- Produces: `proof_from_fingerprints(fingerprints, segment_recipe_hash, compiler_recipe_hash) -> dict[str, object]`
- Produces: `validate_input_proof(value) -> dict[str, object]`
- Produces: `input-proof.json` and `input_proof_sha256` in every schema-3 Generation.

- [ ] **Step 1: Write failing deterministic proof tests**

```python
def test_input_proof_ignores_document_insertion_order() -> None:
    left = proof_from_fingerprints(
        {"note:b": "b" * 64, "note:a": "a" * 64},
        "c" * 64,
        "d" * 64,
    )
    right = proof_from_fingerprints(
        {"note:a": "a" * 64, "note:b": "b" * 64},
        "c" * 64,
        "d" * 64,
    )
    assert canonical_bytes(left) == canonical_bytes(right)


def test_input_proof_changes_with_content_or_recipe() -> None:
    base = proof_from_fingerprints({"note:a": "a" * 64}, "b" * 64, "c" * 64)
    changed = proof_from_fingerprints({"note:a": "d" * 64}, "b" * 64, "c" * 64)
    assert canonical_hash(base) != canonical_hash(changed)
```

- [ ] **Step 2: Verify proof tests fail**

Run:

```powershell
python -m pytest tests/pageindex_v2/test_input_proof.py -q
```

Expected: collection fails because `input_proof.py` is absent.

- [ ] **Step 3: Implement schema-3 recipes and proof payload**

Set `COMPILER_SCHEMA_VERSION = 3` and add:

```python
generation_layout_version: str = "manifest-input-proof-v1"
```

to `CompilerRecipe`, its supported values and `as_dict()`.

The proof schema is exactly:

```python
{
    "schema_version": 1,
    "compiler_recipe_hash": compiler_recipe_hash,
    "documents": {
        doc_key: {
            "content_hash": content_hash,
            "segment_recipe_hash": segment_recipe_hash,
        }
        for doc_key in sorted(documents)
    },
}
```

Reject booleans, unknown keys, empty doc keys and non-64-hex hashes.

- [ ] **Step 4: Bind the proof into compiler output**

Add `input-proof.json` to runtime payloads and add `input_proof_sha256` to the core manifest before calculating `revision_sha256` and `generation_id`. Manifest file metadata must hash and size the proof exactly like other payloads.

- [ ] **Step 5: Run focused proof/compiler tests**

Run:

```powershell
python -m pytest tests/pageindex_v2/test_input_proof.py tests/pageindex_v2/test_compiler.py -q
```

Expected: all tests pass; full and incremental compilation of identical Segments produce the same schema-3 Generation ID.

- [ ] **Step 6: Commit**

```powershell
git add app/index/v2/input_proof.py app/index/v2/models.py app/index/v2/compiler.py tests/pageindex_v2/test_input_proof.py tests/pageindex_v2/test_compiler.py
git commit -m "feat(pageindex): bind source proof to generations"
```

### Task 6: Validate the Bound Input Proof

**Files:**

- Modify: `app/index/v2/validator.py`
- Modify: `tests/pageindex_v2/test_validator.py`

**Interfaces:**

- Consumes: `validate_input_proof()` and manifest `input_proof_sha256`.
- Produces: structural error codes for missing, invalid, unbound or inconsistent proofs.

- [ ] **Step 1: Write failing corruption tests**

Add four independent mutations after materialization:

```python
@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("delete", "input_proof_missing"),
        ("noncanonical", "file_not_canonical"),
        ("change_content_hash", "input_proof_hash_mismatch"),
        ("remove_document", "input_proof_documents_mismatch"),
    ],
)
def test_validator_rejects_invalid_input_proof(...):
    ...
```

Use the existing compiler/materializer fixtures and perform the named concrete file mutation before calling `validate_candidate()`.

- [ ] **Step 2: Verify the tests fail**

Run:

```powershell
python -m pytest tests/pageindex_v2/test_validator.py -q
```

Expected: proof-specific cases fail because the validator does not inspect the new proof.

- [ ] **Step 3: Implement independent proof validation**

The validator must:

1. require `input-proof.json` in manifest files;
2. verify canonical bytes, size and SHA-256;
3. call `validate_input_proof()`;
4. compare its hash with `manifest.input_proof_sha256`;
5. compare its document key set with `manifest.documents`;
6. compare compiler recipe hashes;
7. recompute the schema-3 core manifest and Generation ID.

Deep recompilation must produce the same proof and digest.

- [ ] **Step 4: Run focused validator tests**

Run:

```powershell
python -m pytest tests/pageindex_v2/test_input_proof.py tests/pageindex_v2/test_compiler.py tests/pageindex_v2/test_validator.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add app/index/v2/validator.py tests/pageindex_v2/test_validator.py
git commit -m "feat(pageindex): validate generation input proofs"
```

### Task 7: Return No-change Before Loading Segments

**Files:**

- Modify: `app/index/v2/protocol.py`
- Modify: `app/index/v2/worker.py`
- Modify: `app/index/v2/supervisor.py`
- Modify: `app/index/v2/benchmark.py`
- Modify: `tests/pageindex_v2/test_worker_protocol.py`
- Modify: `tests/pageindex_v2/test_benchmark.py`

**Interfaces:**

- Produces: `VALID_BUILD_OUTCOMES = frozenset({"built", "no_change"})`.
- Produces: `NoChangeMatch(generation_dir, manifest, manifest_sha256, document_count, stabilization_attempts)`.
- Produces: `_try_no_change(request, reporter) -> NoChangeMatch | None`.

- [ ] **Step 1: Write the mechanism-verification test**

```python
def test_no_change_returns_before_loading_or_compiling(
    tmp_path, sample_content, monkeypatch
) -> None:
    pageindex, generation = seed_full_generation(tmp_path, sample_content)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("no-op crossed the incremental boundary")

    monkeypatch.setattr(worker_module, "load_segment", forbidden)
    monkeypatch.setattr(worker_module, "_compile_and_validate", forbidden)
    monkeypatch.setattr(worker_module, "_write_shadow_report", forbidden)

    result = run_incremental_worker(pageindex, sample_content, generation)
    assert result["outcome"] == "no_change"
    assert result["generation"] == generation
    assert result["stats"]["segments_loaded"] == 0
    assert result["stats"]["generation_bytes_written"] == 0
```

Snapshot `objects/` and `generations/` before and after and assert byte-for-byte equality.

- [ ] **Step 2: Write changed-input and compatibility tests**

Cover one modified, one added and one deleted source. Each must return `outcome="built"`. Copy a schema-2 fixture Generation without `input-proof.json`; incremental must use the old path instead of reporting no-change.

- [ ] **Step 3: Verify the tests fail**

Run:

```powershell
python -m pytest tests/pageindex_v2/test_worker_protocol.py tests/pageindex_v2/test_benchmark.py -q
```

Expected: failures because outcome and the pre-load no-op branch do not exist.

- [ ] **Step 4: Implement stable no-change matching**

Call `_try_no_change()` immediately after `accepted` and cancellation checks, before initializing or reading `base_segments`.

`_try_no_change()` must:

1. accept only incremental requests with a base Generation;
2. read and canonical-check the base manifest and proof;
3. verify Generation ID and bound proof hash;
4. verify current recipes;
5. call `discover_documents()` and `fingerprint_document()` for every source;
6. build a live proof;
7. repeat the source snapshot once and require both proofs to match;
8. return `None` for schema 2/no proof or any genuine input difference;
9. raise for a schema-3 manifest that claims a proof but contains a corrupt proof.

- [ ] **Step 5: Return the compatible success result**

```python
{
    "schema_version": PROTOCOL_SCHEMA_VERSION,
    "status": "ready_to_publish",
    "outcome": "no_change",
    "mode": "incremental",
    "base_generation": generation,
    "generation": generation,
    "generation_dir": str(generation_dir),
    "manifest_sha256": manifest_sha256,
    "warnings": [],
    "shadow_report": {"status": "not_run", "reason": "no_change"},
    "stats": {
        "no_op": True,
        "segments_loaded": 0,
        "segments_rebuilt": 0,
        "segments_reused": document_count,
        "segments_deleted": 0,
        "postings_visited": 0,
        "generation_bytes_written": 0,
        "deep_validation_runs": 0,
        "shadow_duration_ms": 0.0,
    },
}
```

Normal builds must add `outcome="built"` and `stats.no_op=False`.

- [ ] **Step 6: Harden supervisor verification**

Reject `no_change` unless mode is incremental, base Generation is present, returned Generation equals base, and the existing Generation manifest still passes canonical/hash/path checks.

- [ ] **Step 7: Run focused no-op tests**

Run:

```powershell
python -m pytest tests/pageindex_v2/test_worker_protocol.py tests/pageindex_v2/test_benchmark.py -q
```

Expected: all tests pass; the benchmark incremental round records `outcome="no_change"`.

- [ ] **Step 8: Commit**

```powershell
git add app/index/v2/protocol.py app/index/v2/worker.py app/index/v2/supervisor.py app/index/v2/benchmark.py tests/pageindex_v2/test_worker_protocol.py tests/pageindex_v2/test_benchmark.py
git commit -m "feat(pageindex): short-circuit unchanged builds"
```

### Task 8: Regression and Capacity Evidence

**Files:**

- Modify: `docs/pageindex-v3-deep-incremental-design.md`
- Modify: `docs/pageindex-v2-incremental-design.md`

**Interfaces:**

- Produces: checked test evidence and a reproducible exact-50k command.

- [ ] **Step 1: Run the focused suite**

```powershell
python -m pytest tests/pageindex_v2/test_process_metrics.py tests/pageindex_v2/test_input_proof.py tests/pageindex_v2/test_benchmark.py tests/pageindex_v2/test_compiler.py tests/pageindex_v2/test_validator.py tests/pageindex_v2/test_worker_protocol.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run all PageIndex tests**

```powershell
python -m pytest tests/pageindex_v2 -q
```

Expected: all tests pass.

- [ ] **Step 3: Run the repository regression suite**

```powershell
python -m pytest -q
```

Expected: all tests pass, allowing only existing documented skips.

- [ ] **Step 4: Run a small real benchmark smoke**

```powershell
python -m app.index.v2.benchmark --content E:\pageindex-v3-smoke\content --pageindex E:\pageindex-v3-smoke\pageindex --full-runs 1 --incremental-runs 3 --synthetic-documents 10 --synthetic-sections 5 --synthetic-words 48 --synthetic-vocabulary 128 --synthetic-seed 42 --require-os-metrics --output E:\pageindex-v3-smoke\result.json
```

Expected: full outcome is `built`; all unchanged incremental outcomes are `no_change`; every round has a distinct PID and non-null Windows metrics.

- [ ] **Step 5: Run the exact-50k gate**

```powershell
python -m app.index.v2.benchmark --content E:\pageindex-v3-bench-50k\content --pageindex E:\pageindex-v3-bench-50k\pageindex --full-runs 1 --incremental-runs 20 --synthetic-profile exact-50k --require-os-metrics --output E:\pageindex-v3-bench-50k\result.json
```

Expected mechanism evidence:

```text
observed_chunks=50000
incremental.outcome=no_change
segments_loaded=0
postings_visited=0
generation_bytes_written=0
deep_validation_runs=0
```

P1 passes its performance gate only if no-op P95 is below 500 ms on the fixed machine. P0/P1 do not claim the single-document or 512 MiB targets; those remain P2/P3 gates.

- [ ] **Step 6: Record measured evidence**

Add the command, corpus hash, machine metric backend, run count, P50/P95, Peak Working Set, Peak Private Bytes observed and I/O totals to the v3 design document. Label a failed gate as failed; do not replace it with an estimate.

- [ ] **Step 7: Final commit**

```powershell
git add docs/pageindex-v3-deep-incremental-design.md docs/pageindex-v2-incremental-design.md
git commit -m "docs(pageindex): record p0 p1 verification evidence"
```

## Self-Review

- Spec coverage: P0 process isolation, OS metrics, exact-50k enforcement and P1 bound proof/no-op are each implemented by one or more tasks. P2-P4 are intentionally excluded from this plan and have independent gates in the v3 design.
- Placeholder scan: the plan contains no unresolved implementation placeholders.
- Type consistency: `OsProcessMetrics`, `ProcessMonitor`, proof helper names, `NoChangeMatch`, `built/no_change` and report field names are consistent across producers, consumers and tests.

## Execution Handoff

The user already authorized execution with “开始推进”. Use subagent-driven implementation where file ownership does not overlap, review each commit boundary, and continue inline when tasks share `worker.py`, `supervisor.py` or benchmark contracts.
