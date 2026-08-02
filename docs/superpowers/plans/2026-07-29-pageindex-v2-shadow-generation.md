# PageIndex v2 Shadow Generation Implementation Plan

**Implementation status (2026-07-29):** Stage A is implemented and verified on the repository corpus. The unchecked boxes below are retained as the original execution sequence, not as current completion state; final evidence is recorded in `docs/pageindex-v2-incremental-design.md` section 31.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic, per-document incremental PageIndex v2 pipeline that writes validated shadow generations without changing the current production read path.

**Architecture:** Discover source documents, fingerprint their complete Markdown inputs, and build immutable content-addressed Segment objects containing stable node identities and unfiltered field postings. Compile all selected Segments into deterministic legacy-compatible JSON files inside a versioned Generation, validate the complete candidate, and execute the pipeline in a short-lived subprocess communicating only through task files.

**Tech Stack:** Python 3.10+, standard library, existing Markdown parser/chunker in `app.vendor.build_pageindex`, shared tokenizer in `app.retrieval.tokenizer`, pytest 8+, JSON task protocol.

## Global Constraints

- Incremental is the primary mode; full rebuild remains an explicit optional mode.
- Segment objects are the sole index fact source.
- `title` and `breadcrumb` postings are never removed by document frequency.
- A `body` posting is removed only when `body_chunk_df >= 256` and `body_chunk_df / total_chunks >= 0.90`.
- Segment and Generation JSON is deterministic compact UTF-8 without BOM.
- Structural, hash, and reference errors fail a candidate; quality and performance differences only produce warnings.
- Stage A writes `objects/segments`, `generations`, and `build`; it does not create or modify `current.json`.
- The legacy files in the PageIndex root remain the active read path during Stage A.
- The worker builds and validates candidates in a short-lived subprocess.
- The existing `node_id` remains the compatibility identifier in exported runtime JSON; stable `node_key` is stored alongside it internally.
- Existing untracked `node_modules/` and `scripts_debug/` content must not be modified or staged.

---

## File Structure

Create:

```text
app/index/v2/
├── __init__.py
├── canonical.py
├── models.py
├── ids.py
├── catalog.py
├── segment_builder.py
├── object_store.py
├── compiler.py
├── validator.py
├── protocol.py
├── worker.py
├── supervisor.py
└── shadow_diff.py
app/pageindex_worker.py
tests/pageindex_v2/
├── conftest.py
├── test_canonical_ids.py
├── test_catalog.py
├── test_segment_builder.py
├── test_object_store.py
├── test_compiler.py
├── test_validator.py
├── test_worker_protocol.py
├── test_incremental_equivalence.py
└── test_shadow_diff.py
```

Modify:

```text
run_app.py
pyproject.toml
docs/pageindex-v2-incremental-design.md
```

Responsibilities:

- `canonical.py`: canonical JSON bytes, SHA-256 helpers, atomic JSON writes.
- `models.py`: versioned recipe constants and immutable document/build records.
- `ids.py`: path/heading normalization and stable `doc_key`/`node_key`.
- `catalog.py`: source discovery, ordered Markdown inputs, content fingerprints.
- `segment_builder.py`: legacy parser adapter, stable nodes/chunks, field TF and lengths.
- `object_store.py`: immutable content-addressed Segment persistence and reuse lookup.
- `compiler.py`: deterministic global IDs, field pruning, compatibility JSON payloads, manifests.
- `validator.py`: independent structural, hash, reference, and pruning checks.
- `protocol.py`: request/progress/result JSON contracts and event writing.
- `worker.py`: Stage A build state machine and candidate materialization.
- `supervisor.py`: development/frozen subprocess invocation and result collection.
- `shadow_diff.py`: semantic normalization and legacy/v2 difference report.
- `app/pageindex_worker.py`: module CLI.
- `run_app.py`: frozen executable `--pageindex-worker` dispatch before desktop imports.

### Task 1: Canonical Serialization, Recipes, and Stable IDs

**Files:**

- Create: `app/index/v2/__init__.py`
- Create: `app/index/v2/canonical.py`
- Create: `app/index/v2/models.py`
- Create: `app/index/v2/ids.py`
- Create: `tests/pageindex_v2/conftest.py`
- Create: `tests/pageindex_v2/test_canonical_ids.py`

**Interfaces:**

- Produces: `canonical_bytes(value: object) -> bytes`
- Produces: `canonical_hash(value: object) -> str`
- Produces: `write_json_atomic(path: Path, value: object) -> None`
- Produces: `SegmentRecipe.as_dict() -> dict[str, object]`
- Produces: `CompilerRecipe.as_dict() -> dict[str, object]`
- Produces: `make_doc_key(doc_type: str, slug: str) -> str`
- Produces: `make_node_key(doc_key: str, source_path: str, breadcrumb: Sequence[str], duplicate_ordinal: int) -> str`
- Produces: `normalize_relative_path(path: str) -> str`

- [ ] **Step 1: Write failing canonical and ID tests**

```python
def test_canonical_hash_ignores_mapping_insertion_order() -> None:
    assert canonical_hash({"b": 2, "a": 1}) == canonical_hash({"a": 1, "b": 2})


def test_node_key_is_cross_platform_stable() -> None:
    left = make_node_key("book:demo", r"books\demo\ch01.md", [" Demo ", "A  B"], 0)
    right = make_node_key("book:demo", "books/demo/ch01.md", ["Demo", "A B"], 0)
    assert left == right
    assert left.startswith("n_")
```

- [ ] **Step 2: Run tests and verify missing-module failure**

Run: `python -m pytest tests/pageindex_v2/test_canonical_ids.py -q`

Expected: collection fails because `app.index.v2` does not exist.

- [ ] **Step 3: Implement deterministic primitives**

Use these exact serialization rules:

```python
json.dumps(
    value,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
    allow_nan=False,
).encode("utf-8")
```

Use Unicode NFC, slash-separated relative paths, collapsed heading whitespace, SHA-256, `n_` plus 24 hexadecimal characters for node keys, and 64 hexadecimal characters for stored object hashes.

- [ ] **Step 4: Run the focused tests**

Run: `python -m pytest tests/pageindex_v2/test_canonical_ids.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/index/v2 tests/pageindex_v2
git commit -m "feat(pageindex): add deterministic v2 primitives"
```

### Task 2: Document Catalog and Content Fingerprints

**Files:**

- Create: `app/index/v2/catalog.py`
- Create: `tests/pageindex_v2/test_catalog.py`

**Interfaces:**

- Consumes: `normalize_relative_path`, `canonical_hash`
- Produces: `DocumentSource(doc_type, slug, doc_key, root, files)`
- Produces: `discover_documents(content_dir: Path) -> tuple[DocumentSource, ...]`
- Produces: `fingerprint_document(source: DocumentSource) -> str`

- [ ] **Step 1: Write source-discovery and deletion tests**

```python
def test_discover_documents_ignores_section_indexes(sample_content: Path) -> None:
    docs = discover_documents(sample_content)
    assert [d.doc_key for d in docs] == [
        "book:alpha",
        "paper:beta",
        "note:welcome",
    ]


def test_fingerprint_changes_when_a_chapter_is_deleted(sample_content: Path) -> None:
    source = discover_documents(sample_content)[0]
    before = fingerprint_document(source)
    (sample_content / "books" / "alpha" / "ch02.md").unlink()
    source_after = discover_documents(sample_content)[0]
    assert fingerprint_document(source_after) != before
```

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest tests/pageindex_v2/test_catalog.py -q`

Expected: imports fail because `catalog.py` is absent.

- [ ] **Step 3: Implement deterministic discovery**

Discovery rules:

```text
books/<slug>/_index.md plus all sibling *.md files
papers/<slug>/_index.md
notes/<slug>.md excluding notes/_index.md
```

Sort documents by `(doc_type_order, slug)` where the order is `book`, `paper`, `note`. Fingerprint a canonical array of `{path, sha256}` records so renamed, added, modified, reordered, and deleted Markdown inputs invalidate the document.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/pageindex_v2/test_catalog.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/index/v2/catalog.py tests/pageindex_v2/test_catalog.py
git commit -m "feat(pageindex): discover and fingerprint documents"
```

### Task 3: Per-Document Segment Builder

**Files:**

- Create: `app/index/v2/segment_builder.py`
- Create: `tests/pageindex_v2/test_segment_builder.py`

**Interfaces:**

- Consumes: `DocumentSource`, `SegmentRecipe`, stable ID helpers
- Consumes: `app.retrieval.tokenizer.tokenize`
- Reuses: Markdown parsing and `split_into_chunks` behavior from `app.vendor.build_pageindex`
- Produces: `build_segment(source: DocumentSource, recipe: SegmentRecipe) -> dict[str, object]`

- [ ] **Step 1: Write failing Segment contract tests**

```python
def test_segment_keeps_unfiltered_field_tf(sample_content: Path) -> None:
    source = discover_documents(sample_content)[0]
    segment = build_segment(source, SegmentRecipe())
    assert segment["schema_version"] == 2
    assert segment["document"]["doc_key"] == "book:alpha"
    posting = segment["postings"]["common"]
    assert all(len(item) == 4 for item in posting)


def test_segment_nodes_keep_stable_and_legacy_ids(sample_content: Path) -> None:
    source = discover_documents(sample_content)[0]
    segment = build_segment(source, SegmentRecipe())
    assert all(node["node_key"].startswith("n_") for node in segment["nodes"])
    assert all(node["legacy_node_id"] for node in segment["nodes"])
```

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest tests/pageindex_v2/test_segment_builder.py -q`

Expected: imports fail because `segment_builder.py` is absent.

- [ ] **Step 3: Implement the parser adapter and Segment schema**

The Segment must contain:

```python
{
    "schema_version": 2,
    "document": {...},
    "fingerprint": {
        "content_hash": "...",
        "recipe_hash": "...",
        "source_files": [...],
    },
    "nodes": [...],
    "chunks": [...],
    "postings": {
        token: [[local_id, title_tf, breadcrumb_tf, body_tf], ...],
    },
    "document_tree": {...},
}
```

Build field TF independently using the shared tokenizer. Keep `legacy_node_id` for compatible export and use `node_key` as the stable internal identity. Assign unique `local_id` values after sorting chunk records by `(node_key, node_local_ordinal)`.

- [ ] **Step 4: Add deterministic rebuild and source-range assertions**

```python
def test_rebuilding_segment_produces_identical_bytes(sample_content: Path) -> None:
    source = discover_documents(sample_content)[0]
    assert canonical_bytes(build_segment(source, SegmentRecipe())) == canonical_bytes(
        build_segment(source, SegmentRecipe())
    )
```

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest tests/pageindex_v2/test_segment_builder.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add app/index/v2/segment_builder.py tests/pageindex_v2/test_segment_builder.py
git commit -m "feat(pageindex): build immutable field-aware segments"
```

### Task 4: Content-Addressed Store and Deterministic Compiler

**Files:**

- Create: `app/index/v2/object_store.py`
- Create: `app/index/v2/compiler.py`
- Create: `tests/pageindex_v2/test_object_store.py`
- Create: `tests/pageindex_v2/test_compiler.py`

**Interfaces:**

- Produces: `put_segment(pageindex_dir: Path, segment: Mapping[str, object]) -> StoredSegment`
- Produces: `load_segment(pageindex_dir: Path, segment_hash: str) -> dict[str, object]`
- Produces: `find_reusable_segments(pageindex_dir: Path) -> dict[tuple[str, str, str], str]`
- Produces: `compile_generation(segments: Sequence[Mapping[str, object]], recipe: CompilerRecipe) -> CompiledGeneration`
- Produces: `should_prune_body(body_df: int, total_chunks: int) -> bool`

- [ ] **Step 1: Write failing immutability and pruning tests**

```python
def test_put_segment_is_content_addressed(tmp_path: Path, segment: dict) -> None:
    first = put_segment(tmp_path, segment)
    second = put_segment(tmp_path, segment)
    assert first == second
    assert first.path.read_bytes() == canonical_bytes(segment)


@pytest.mark.parametrize(
    ("df", "chunks", "expected"),
    [(255, 255, False), (256, 1000, False), (900, 1000, True), (256, 256, True)],
)
def test_body_pruning_uses_both_thresholds(df: int, chunks: int, expected: bool) -> None:
    assert should_prune_body(df, chunks) is expected
```

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest tests/pageindex_v2/test_object_store.py tests/pageindex_v2/test_compiler.py -q`

Expected: imports fail because the store and compiler are absent.

- [ ] **Step 3: Implement immutable object storage**

Write Segment bytes to:

```text
objects/segments/<hash[0:2]>/<full-hash>.json
```

If the destination exists, verify its bytes match instead of rewriting it. Reject malformed hashes and path traversal.

- [ ] **Step 4: Implement deterministic compilation**

Compiler ordering:

```text
documents: book slug, paper slug, note slug
nodes: document order then legacy DFS order
chunks: doc_key, node_key, local_id
postings: token lexical order, then numeric chunk ID
```

Compatibility outputs:

```text
global-index.json
node-index.json
chunks.json
inverted-index.json
books/<slug>.json
papers/<slug>.json
notes/<slug>.json
```

For each token/chunk, export total TF as:

```python
title_tf + breadcrumb_tf + (0 if should_prune_body(body_df, total_chunks) else body_tf)
```

Do not remove a posting when the retained title or breadcrumb TF is nonzero.

- [ ] **Step 5: Add incremental/full semantic equality test**

Build the same final Segment set in different insertion orders and assert equal:

```python
assert compile_generation(forward, recipe).generation_id == compile_generation(
    reversed(forward), recipe
).generation_id
```

- [ ] **Step 6: Run focused tests**

Run: `python -m pytest tests/pageindex_v2/test_object_store.py tests/pageindex_v2/test_compiler.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add app/index/v2/object_store.py app/index/v2/compiler.py tests/pageindex_v2
git commit -m "feat(pageindex): compile deterministic shadow generations"
```

### Task 5: Candidate Materialization and Structural Validator

**Files:**

- Create: `app/index/v2/validator.py`
- Create: `tests/pageindex_v2/test_validator.py`

**Interfaces:**

- Consumes: stored Segments and `CompiledGeneration`
- Produces: `materialize_candidate(candidate_dir: Path, compiled: CompiledGeneration) -> Path`
- Produces: `validate_candidate(candidate_dir: Path, pageindex_dir: Path) -> ValidationReport`

- [ ] **Step 1: Write failing corruption and reference tests**

```python
def test_validator_rejects_dangling_posting(candidate: Path, pageindex_root: Path) -> None:
    inverted = read_json(candidate / "inverted-index.json")
    inverted["postings"]["broken"] = [[999999, 1]]
    write_json_atomic(candidate / "inverted-index.json", inverted)
    report = validate_candidate(candidate, pageindex_root)
    assert not report.ok
    assert "posting_unknown_chunk" in report.error_codes
```

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest tests/pageindex_v2/test_validator.py -q`

Expected: imports fail because `validator.py` is absent.

- [ ] **Step 3: Implement hard validation**

Reject:

- unknown schema/recipe;
- missing manifest files;
- Segment object hash mismatch;
- document/Segment set mismatch;
- unknown document/node/chunk references;
- duplicate chunk IDs;
- dangling postings;
- title or breadcrumb DF pruning;
- body pruning outside the `256 and 90%` rule;
- invalid JSON;
- file hash or byte-size mismatch.

Warnings remain data, never exceptions, for quality, latency, size, empty summaries, and recall differences.

- [ ] **Step 4: Add atomic candidate finalization**

Build in `build/<job-id>/candidate`, validate there, then atomically rename it to `generations/<generation-id>`. If the Generation already exists, verify equality and reuse it.

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest tests/pageindex_v2/test_validator.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add app/index/v2/validator.py tests/pageindex_v2/test_validator.py
git commit -m "feat(pageindex): validate and finalize generations"
```

### Task 6: Worker Task Protocol and Subprocess Supervisor

**Files:**

- Create: `app/index/v2/protocol.py`
- Create: `app/index/v2/worker.py`
- Create: `app/index/v2/supervisor.py`
- Create: `app/pageindex_worker.py`
- Modify: `run_app.py`
- Modify: `pyproject.toml`
- Create: `tests/pageindex_v2/test_worker_protocol.py`

**Interfaces:**

- Produces: `BuildRequest.from_dict(value: Mapping[str, object]) -> BuildRequest`
- Produces: `run_worker(request_path: Path) -> int`
- Produces: `run_shadow_build(content_dir: Path, pageindex_dir: Path, mode: str) -> dict[str, object]`

- [ ] **Step 1: Write failing protocol tests**

```python
def test_worker_rejects_unknown_mode(tmp_path: Path) -> None:
    request = tmp_path / "request.json"
    write_json_atomic(request, {"schema_version": 1, "mode": "patch"})
    assert run_worker(request) == 2
    assert read_json(tmp_path / "result.json")["status"] == "failed"
```

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest tests/pageindex_v2/test_worker_protocol.py -q`

Expected: imports fail because Worker modules are absent.

- [ ] **Step 3: Implement the task-file state machine**

Request fields:

```json
{
  "schema_version": 1,
  "job_id": "idx_<hex>",
  "mode": "incremental",
  "content_dir": "absolute path",
  "pageindex_dir": "absolute path",
  "base_generation": null
}
```

Write atomic `progress.json`, append `events.jsonl`, write a final `result.json`, and honor `cancel.request` at safe stage boundaries. Return exit code `0` for a validated Generation, `2` for invalid request, `3` for cancellation, and `1` for build or validation failure.

- [ ] **Step 4: Implement Segment reuse**

For incremental mode, index existing Generation manifests by:

```text
(doc_key, content_hash, segment_recipe_hash) -> segment_hash
```

Reuse exact matches, rebuild only unmatched documents, and omit deleted documents from the new core manifest. Full mode rebuilds every Segment. Recompile requires `base_generation` and reuses all referenced Segments.

- [ ] **Step 5: Implement development and frozen launch commands**

Development:

```python
[sys.executable, "-m", "app.pageindex_worker", str(request_path)]
```

Frozen:

```python
[sys.executable, "--pageindex-worker", str(request_path)]
```

`run_app.py` must inspect `sys.argv` before importing `app.main`.

- [ ] **Step 6: Fix package discovery**

Replace the explicit top-level-only package list with setuptools discovery that includes `app*`.

- [ ] **Step 7: Run Worker tests**

Run: `python -m pytest tests/pageindex_v2/test_worker_protocol.py -q`

Expected: all tests pass, including a real subprocess round trip.

- [ ] **Step 8: Commit**

```bash
git add app/index/v2 app/pageindex_worker.py run_app.py pyproject.toml tests/pageindex_v2
git commit -m "feat(pageindex): run v2 builds in a worker process"
```

### Task 7: Semantic Shadow Difference Report

**Files:**

- Create: `app/index/v2/shadow_diff.py`
- Create: `tests/pageindex_v2/test_shadow_diff.py`

**Interfaces:**

- Produces: `compare_legacy_to_generation(legacy_dir: Path, generation_dir: Path) -> dict[str, object]`

- [ ] **Step 1: Write failing semantic-ID tests**

```python
def test_diff_ignores_global_chunk_renumbering(legacy_dir: Path, generation_dir: Path) -> None:
    report = compare_legacy_to_generation(legacy_dir, generation_dir)
    assert report["chunks"]["semantic_mismatch"] == 0
    assert report["chunks"]["id_only_changes"] > 0
```

- [ ] **Step 2: Verify the test fails**

Run: `python -m pytest tests/pageindex_v2/test_shadow_diff.py -q`

Expected: import fails because `shadow_diff.py` is absent.

- [ ] **Step 3: Implement normalization**

Compare:

- documents by `type:id`;
- nodes by mapped stable `doc_key + node_key`;
- chunks by `doc_key + node_key + local ordinal`, with body hash as diagnostic;
- postings by semantic chunk key and reconstructed field TF;
- only document-tree files referenced by `global-index.json`;
- stale legacy tree files as a separate count;
- index bytes, counts, build duration, and warning totals.

Classify body-only differences satisfying the v2 threshold as `expected_pruned`. Treat title or breadcrumb posting loss as a structural error.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/pageindex_v2/test_shadow_diff.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/index/v2/shadow_diff.py tests/pageindex_v2/test_shadow_diff.py
git commit -m "feat(pageindex): add semantic shadow diff reports"
```

### Task 8: End-to-End Equivalence, Fault Tests, and Real-Corpus Build

**Files:**

- Create: `tests/pageindex_v2/test_incremental_equivalence.py`
- Modify: `docs/pageindex-v2-incremental-design.md`

**Interfaces:**

- Consumes: public Worker/Supervisor interface only.
- Produces: repeatable full/incremental equivalence coverage and real-corpus verification commands.

- [ ] **Step 1: Write full/incremental equivalence tests**

Exercise:

```text
initial full
single Markdown edit + incremental
same final corpus + clean full
document addition
document deletion
worker cancellation
corrupt Segment object
corrupt candidate manifest
```

Assert that incremental and clean full produce the same Generation ID for the same final corpus and recipes.

- [ ] **Step 2: Run all v2 tests**

Run: `python -m pytest tests/pageindex_v2 -q`

Expected: all tests pass.

- [ ] **Step 3: Run legacy and full regression suites**

Run:

```text
python -m pytest tests/test_build_pageindex_docdf.py -q
python -m pytest tests/retrieval/test_py_retrieval.py -q
python -m pytest -q
```

Expected: legacy PageIndex tests and the complete existing suite remain green.

- [ ] **Step 4: Build the repository’s real corpus in Shadow mode**

Run:

```text
python -m app.index.v2.supervisor full --content data/content --pageindex data/pageindex
python -m app.index.v2.supervisor incremental --content data/content --pageindex data/pageindex
```

Expected:

- validated Generation directory exists;
- the second build reuses all unchanged Segments;
- full and incremental report the same Generation ID;
- the PageIndex root legacy files are byte-for-byte unchanged;
- the report lists the 59 currently stale legacy document-tree files separately.

- [ ] **Step 5: Record implementation refinements**

Update the design document with the finalized normalization algorithm, 24-hex node key length, Worker exit codes, package paths, and exact Shadow invocation. Keep the document status as implemented only for Stage A.

- [ ] **Step 6: Final regression and repository boundary check**

Run:

```text
python -m pytest -q
git diff --check
git status --short
```

Expected: tests pass; no whitespace errors; only PageIndex v2 implementation, tests, and documentation are modified or added.

- [ ] **Step 7: Commit**

```bash
git add app tests/pageindex_v2 run_app.py pyproject.toml docs
git commit -m "test(pageindex): verify v2 shadow generation end to end"
```
