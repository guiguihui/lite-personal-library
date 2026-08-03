# PageIndex V3 Search Convergence Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make PageIndex V3 the single build, status, search, chat, and knowledge-link runtime while keeping the desktop application local-first and lightweight.

**Architecture:** Add a small application runtime around V3's immutable Generation/View artifacts. A single atomic `current-v3.json` publishes the trusted pair; `/api/search` opens that exact pin, index status validates that same pin, and chat consumes only the HTTP search contract. Knowledge-link indexing reads Markdown directly and is rebuilt only after a V3 publication succeeds.

**Tech Stack:** Python 3.11+, FastAPI, PageIndex V3 immutable artifacts, vanilla JavaScript, PyWebView, pytest, Node test runner.

---

### Task 1: Add the V3 publication runtime

**Files:**
- Create: `app/index/v3/runtime.py`
- Test: `tests/pageindex_v3/test_runtime.py`

- [ ] Write failing tests for atomic publication, strict pin loading, missing/corrupt pointers, and opening the exact pinned view.
- [ ] Implement `load_current`, `publish_current`, `open_current_view`, and `is_ready` around `current-v3.json`.
- [ ] Reconstruct and authenticate the compact logical Generation receipt from immutable artifacts.
- [ ] Run `pytest tests/pageindex_v3/test_runtime.py -q`.

### Task 2: Move index build and application status to V3

**Files:**
- Modify: `app/index/status.py`
- Modify: `app/http/routes_status.py`
- Modify: `app/http/schemas.py`
- Test: `tests/http/test_status_v3.py`

- [ ] Write failing tests proving build invokes V3 without legacy export and status is derived from the current V3 pin.
- [ ] Map the existing `full` request to a V3 bootstrap and `incremental` to the current trusted parent.
- [ ] Publish successful V3 results and expose generation/view identifiers in status.
- [ ] Run the targeted status/build tests.

### Task 3: Make `/api/search` the only retrieval implementation

**Files:**
- Modify: `app/http/routes_search.py`
- Modify: `app/http/schemas.py`
- Modify: `app/retrieval/search_view.py`
- Replace: `tests/http/test_search_view_shadow.py`

- [ ] Write failing HTTP tests proving results come from the current V3 view with stable source metadata and no legacy fallback.
- [ ] Remove legacy index loading, shadow comparison, and duplicate retrieval/ranking from the route.
- [ ] Return source path and line boundaries needed for frontend context assembly.
- [ ] Run the search route and V3 retrieval tests.

### Task 4: Slim chat to API retrieval and context assembly

**Files:**
- Modify: `frontend/chat/agent.js`
- Modify: `frontend/index.html`
- Create: `tests/frontend/chat-search-api.test.js`

- [ ] Write a frontend test that rejects requests for PageIndex JSON files and expects `/api/search`.
- [ ] Replace local search/RM3/rerank/MMR with an async `/api/search` client that preserves backend order.
- [ ] Keep only Agent tool orchestration, source fetching, context-budget packing, citations, and UI behavior.
- [ ] Remove the chat retrieval bundle from the page and run frontend tests.

### Task 5: Attach knowledge links to the V3 lifecycle

**Files:**
- Modify: `app/knowledge/catalog.py`
- Modify: `app/knowledge/build_hook.py`
- Modify: `app/index/status.py`
- Test: `tests/knowledge/test_v3_build_hook.py`

- [ ] Write failing tests showing link-index construction works without legacy per-document index JSON.
- [ ] Extract heading anchors directly from Markdown sources.
- [ ] Rebuild links after a successful V3 publication and surface link-build failures in the job result.
- [ ] Run knowledge-link and HTTP link tests.

### Task 6: Rewrite architecture documentation and verify the application

**Files:**
- Modify: `docs/architecture.md`

- [ ] Document the V3-only runtime, `/api/search` boundary, frontend responsibilities, local storage layout, build/publication sequence, and retained V2 primitives.
- [ ] Run the full Python and frontend test suites.
- [ ] Start the desktop application, build/publish V3 if needed, and verify `/api/status`, `/api/search`, chat startup, and knowledge links.
