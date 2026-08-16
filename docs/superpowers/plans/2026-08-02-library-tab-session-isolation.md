# Library Tab Session Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Make every library tab own its document, DOM, and request state so opening or reusing Wikilinks and graph nodes always renders content that matches the selected tab.

**Architecture:** Introduce a small session module keyed by `tab.id`, then pass the active session explicitly through the library shelf/tree/reader loaders. Library tab activation becomes the only navigation source of truth; activity-bar synchronization reflects the active tab without activating another tab. Existing public entry points such as `LqdLibrary.openDoc(type, slug, nodeId)` and `window.selectDoc(type, slug)` remain compatible.

**Tech Stack:** Browser JavaScript (ES5-compatible IIFEs), FastAPI-served static assets, Node built-in test runner, pytest, Codex in-app browser.

## Global Constraints

- Work only on `feat/knowledge-linking-governance-context`.
- Do not modify real Markdown holdings under `data/content`.
- Do not add a runtime dependency.
- Preserve search, Wikilink, backlink, graph-node, chat, upload, manage, and configuration entry points.
- Keep `node_modules/` and `scripts_debug/` untouched.

---

### Task 1: Isolated Library Session Primitive

**Files:**
- Create: `frontend/library/session.js`
- Create: `tests/frontend/library-session.test.js`
- Modify: `frontend/index.html`
- Modify: `tests/test_frontend_knowledge.py`

**Interfaces:**
- Produces: `window.LqdLibrarySessions.create(tabId, initialState)`, `begin(session)`, `isCurrent(session, token)`, `bind(session, refs)`, `dispose(tabId)`, and `get(tabId)`.
- A session stores `tabId`, normalized `type`, `slug`, `nodeId`, DOM references, request version, and an optional `AbortController`.

- [x] **Step 1: Write the failing session unit tests**

```js
test('sessions isolate document state by tab id', () => {
  const a = sessions.create('tab-a', { type: 'note', slug: 'a' });
  const b = sessions.create('tab-b', { type: 'paper', slug: 'b' });
  assert.equal(a.type, 'notes');
  assert.equal(b.type, 'papers');
  assert.notEqual(a, b);
});

test('begin invalidates the previous request token', () => {
  const session = sessions.create('tab-a', {});
  const first = sessions.begin(session);
  const second = sessions.begin(session);
  assert.equal(sessions.isCurrent(session, first.version), false);
  assert.equal(sessions.isCurrent(session, second.version), true);
});
```

- [x] **Step 2: Run the focused test and confirm failure**

Run: `node --test tests/frontend/library-session.test.js`

Expected: FAIL because `frontend/library/session.js` does not exist.

- [x] **Step 3: Implement the session registry and request invalidation**

Implement an IIFE that exports to both `window.LqdLibrarySessions` and `module.exports`, normalizes singular document types, aborts the previous controller in `begin`, and clears DOM references in `dispose`.

- [x] **Step 4: Load the session module before `reader.js` and extend syntax tests**

Add `/frontend/library/session.js` before `/frontend/library/reader.js` in `frontend/index.html`; run it through `node --check` in `tests/test_frontend_knowledge.py`.

- [x] **Step 5: Run focused tests**

Run: `node --test tests/frontend/library-session.test.js tests/frontend/wikilinks.test.js`

Expected: all tests pass.

### Task 2: Refactor the Library Reader Around Sessions

**Files:**
- Create: `frontend/library/reader.js`
- Modify: `frontend/library/index.js`
- Test: `tests/frontend/library-session.test.js`

**Interfaces:**
- Consumes: `LqdLibrarySessions` from Task 1.
- Produces: `window.initLibrary(container, tab)`, `window.unmountLibrary(tab)`, and compatibility wrapper `window.selectDoc(type, slug)`.

- [x] **Step 1: Add a failing lifecycle test**

Extend the session test to assert that disposing `tab-a` aborts its active request without changing `tab-b`.

- [x] **Step 2: Refactor render and load functions**

Change `renderShelf`, `renderTree`, `renderReaderHeader`, `renderSection`, `loadDocs`, `selectDoc`, and `selectSection` to accept a `session` argument and read only `session.shelfEl`, `session.treeEl`, `session.readerEl`, and session document state.

- [x] **Step 3: Make every asynchronous completion session-safe**

Every fetch uses the token returned by `LqdLibrarySessions.begin(session)` and ignores success or failure when `isCurrent` is false. Pass `signal` when an `AbortController` is available.

- [x] **Step 4: Persist document and node state to the owning tab**

On document and section selection call:

```js
window.LqdTabs.updateTabState(session.tabId, {
  type: session.type,
  slug: session.slug,
  nodeId: session.nodeId
});
```

Do not look up `LqdTabs.active()` from an asynchronous callback.

- [x] **Step 5: Mount and unmount by tab identity**

`frontend/library/index.js` calls `initLibrary(container, tab)` once and calls `unmountLibrary(tab)` from `unmount`. The mount restores `tab.state.type`, `slug`, and `nodeId` without launching a competing default request.

- [x] **Step 6: Run syntax and frontend tests**

Run: `python -m pytest tests/test_frontend_knowledge.py -q`

Expected: PASS.

### Task 3: Make Active Tab the Navigation Source of Truth

**Files:**
- Modify: `frontend/core/shell.js`
- Modify: `frontend/core/tabs.js`
- Modify: `frontend/library/open-doc.js`
- Test: `tests/frontend/library-session.test.js`

**Interfaces:**
- Consumes: library tabs with stable `{type, slug, nodeId}` state.
- Produces: `LqdShell.reflectActiveTab(tab)` that updates activity, sidebar, and overview without calling `LqdTabs.activate`.

- [x] **Step 1: Separate activity commands from activity reflection**

Keep `setActivity(activity)` for user activity-bar clicks. Add `reflectActiveTab(tab)` for `tab:opened` and `tab:activated`; it updates the activity store and panels but never searches for or activates a tab.

- [x] **Step 2: Ensure activate always mounts when content is stale**

Track the mounted tab ID in `tabs.js`. If `activeTabId === id` but the mounted ID differs, remount instead of returning early. Clear the mounted ID when the main container is replaced or the tab is closed.

- [x] **Step 3: Keep `openDoc` atomic and compatible**

Normalize types, update an existing matching tab state, activate it once, and emit `library:node:select` only when the already-mounted tab needs an in-place node jump. New tabs restore entirely from `tab.state`.

- [x] **Step 4: Reserve restored tab IDs before allocating new tabs**

Use `LqdTabIds.reserve(id)` for restored explicit IDs and skip all occupied IDs in `next(exists)`, preventing restored and newly opened tabs from sharing an identity.

- [x] **Step 5: Run frontend tests**

Run: `python -m pytest tests/test_frontend_knowledge.py -q`

Expected: PASS.

### Task 4: Automated and Manual Regression

**Files:**
- Modify: `tests/ui/serve_knowledge_acceptance.py`
- Create: `docs/acceptance/screenshots/knowledge-links-2026-08-02/10-existing-tab-reuse-fixed.png`
- Modify: `docs/acceptance/knowledge-links-ui-acceptance-2026-08-02.md`

**Interfaces:**
- Consumes: completed session and navigation refactor.
- Produces: browser evidence that selected tab, activity bar, overview, and reader agree.

- [x] **Step 1: Run full automated regression**

Run: `python -m pytest -q`

Expected: `260 passed, 1 skipped` or higher with no failures.

- [x] **Step 2: Run the exact previously failing browser sequence**

Execute:

```text
Alpha note → graph-paper Wikilink → Alpha graph node
→ graph-paper Wikilink again → reuse graph-paper
```

- [x] **Step 3: Assert visible invariants**

Confirm exactly one `graph-paper` tab exists, the Library activity is pressed, the reader/overview title is `Quartz 图谱论文`, and the chat composer is absent.

- [x] **Step 4: Save final screenshot and update the report**

Save `10-existing-tab-reuse-fixed.png`, change the remaining blocker to passed, and record any unrelated residual risk separately.

## Self-Review

- Spec coverage: session isolation, compatibility entry points, request cancellation, activity synchronization, tab reuse, persistence state, automated tests, and browser evidence are covered.
- Placeholder scan: no deferred implementation placeholders remain.
- Type consistency: document types are normalized to `books`, `papers`, and `notes`; public identifiers remain canonical `book:`, `paper:`, and `note:` IDs at API boundaries.
