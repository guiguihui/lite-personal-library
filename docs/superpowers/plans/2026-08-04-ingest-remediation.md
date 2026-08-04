# Ingest Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修通 PDF/EPUB 文件导入、V3 文档库、离线处理、文本质量、失败队列和桌面聊天布局，并以完整自动化回归和桌面验收作为提交门槛。

**Architecture:** 浏览器文件使用 multipart 流式上传，桌面绝对路径保留兼容但统一同步预检；EPUB 使用 PyMuPDF 保底、Pandoc 可选增强。Library 只读取已发布 V3 的 authenticated Segment 投影；提取引擎与网络策略正交，文本在提取和检索两侧保守规范化。

**Tech Stack:** Python 3.10、FastAPI、Pydantic 2、PyMuPDF、pywebview、原生 JavaScript、Node `node:test`、pytest、PageIndex V3。

## Global Constraints

- 不引入数据库、远程服务、前端整套索引下载或新的重量级运行时。
- 自动化使用 `tmp_path`，不得修改用户正式 `data`。
- V3 不可用时 fail closed，禁止自动回退 Legacy JSON。
- `offline` 除 loopback 外不得发起网络请求。
- 用户文件、slug、路径和节点参数必须在服务端校验。
- 当前 `app/main.py` 的既有 DevTools 关闭改动必须保留，直到最终范围审查。
- 用户要求所有测试完成后再提交，因此本计划不做中间 commit；最终只提交相关文件。

---

## File Structure

- Create `app/ingest/preflight.py`: 安全文件名、slug、格式签名、策略和源路径同步预检。
- Create `app/ingest/upload_store.py`: 分块上传、SHA-256、原子暂存和清理。
- Create `app/pdf/epub_fitz.py`: 无 Pandoc 时的 EPUB 本地兼容提取。
- Modify `app/pdf/epub.py`: Pandoc resolver 与高保真实现。
- Modify `app/pdf/factory.py`: 能力驱动的 PDF/EPUB 路由，禁用 DOCX 占位。
- Modify `app/http/schemas.py`, `app/http/routes_ingest.py`: 新请求模型、capabilities、upload、同步预检和错误结构。
- Modify `app/ingest/jobs.py`, `pipeline.py`, adapters: 新字段、清理、build 状态和网络策略。
- Create `app/library/__init__.py`, `app/library/v3_service.py`: V3 文档列表、阅读、章节和 pin 一致性。
- Modify `app/index/v3/segment_projection.py`, `reader.py`: authenticated document projection 公共接口。
- Modify `app/http/routes_content.py`: V3-only Library API。
- Create `app/text/__init__.py`, `app/text/normalization.py`: 展示和检索规范化。
- Modify `app/pdf/local.py`, `app/retrieval/tokenizer.py`: 连字 flags 和共同检索规范化。
- Modify `app/ingest/clean_adapter.py`, `app/vendor/clean_markdown.py`: 显式 clean classifier 策略。
- Modify `frontend/upload/*`, `frontend/library/reader.js`, `frontend/chat/*`, `frontend/core/shell.css`, `frontend/manage/manage.js`: 新接口、队列、pin、布局。
- Add focused Python and Node tests named in the technical specification.
- Update `docs/architecture.md`, `docs/development.md` and the QA/spec/plan documents.

### Task 1: Upload, Preflight, Capabilities, and EPUB Fallback

**Files:**
- Create: `app/ingest/preflight.py`
- Create: `app/ingest/upload_store.py`
- Create: `app/pdf/epub_fitz.py`
- Modify: `app/pdf/epub.py`
- Modify: `app/pdf/factory.py`
- Modify: `app/http/schemas.py`
- Modify: `app/http/routes_ingest.py`
- Modify: `app/ingest/jobs.py`
- Test: `tests/test_ingest_upload.py`, `tests/test_pdf.py`, `tests/test_http_api.py`

**Interfaces:**
- Produces: `preflight_source(path, *, pdfs_dir, slug, strategy, network_policy, stages) -> PreflightResult`
- Produces: `UploadStore.stage(upload, filename) -> StagedUpload`
- Produces: `get_ingest_capabilities() -> dict[str, object]`
- Produces: `POST /api/ingest/upload`, `GET /api/ingest/capabilities`

- [ ] **Step 1: Write failing tests for sanitization, signatures, capabilities, and upload atomicity**

```python
def test_relative_browser_name_is_not_a_valid_source(tmp_path):
    with pytest.raises(PreflightError) as exc:
        preflight_source(Path("book.epub"), pdfs_dir=tmp_path, slug="book", strategy="local", network_policy="offline", stages=("extract",))
    assert exc.value.code == "SOURCE_NOT_FOUND"

def test_epub_capability_falls_back_to_fitz(monkeypatch):
    monkeypatch.setattr("app.pdf.epub.resolve_pandoc", lambda: None)
    assert get_ingest_capabilities()["formats"]["epub"]["available"] is True
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python -m pytest tests/test_ingest_upload.py tests/test_pdf.py tests/test_http_api.py -k "upload or capability or epub" -q`

Expected: FAIL because the upload/preflight interfaces do not exist.

- [ ] **Step 3: Implement immutable preflight results and structured errors**

```python
@dataclass(frozen=True, slots=True)
class PreflightResult:
    source_path: Path
    extension: str
    strategy: str
    network_policy: str

class PreflightError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 422): ...
```

- [ ] **Step 4: Implement chunked `UploadStore` and multipart route**

Use 1 MiB reads, `*.part`, SHA-256, `os.replace`, 512 MiB default limit, and cleanup-on-error. Parse the `request` form field with `IngestUploadRequest.model_validate_json()` before job creation.

- [ ] **Step 5: Implement `EpubFitzExtractor` and engine selection**

```python
def make_extractor(filename, strategy=None):
    if suffix == ".epub":
        return EpubExtractor() if resolve_pandoc() else EpubFitzExtractor()
```

Return `source_format="epub"`, metadata, page count, warnings, and Markdown output. Remove `.docx` from advertised/supported extensions until a real extractor exists.

- [ ] **Step 6: Run focused tests to green**

Run: `python -m pytest tests/test_ingest_upload.py tests/test_pdf.py tests/test_http_api.py -k "upload or capability or epub or ingest" -q`

Expected: PASS.

### Task 2: Frontend Upload Flow and Queue State Machine

**Files:**
- Modify: `frontend/upload/upload.js`
- Modify: `frontend/upload/upload-queue.js`
- Modify: `frontend/upload/upload.css`
- Modify: `frontend/manage/manage.js`
- Modify: `app/main.py`
- Test: `tests/frontend/upload-queue.test.js`, `tests/frontend/upload-flow.test.js`, `tests/test_frontend_knowledge.py`

**Interfaces:**
- Consumes: `/api/ingest/upload`, `/api/ingest/capabilities`, structured errors.
- Produces: `clearByStatus(statuses)`, `retry(id)`, `retryAllFailed()`, `counts()`.

- [ ] **Step 1: Write failing Node tests for FormData routing and queue transitions**

```javascript
test('retry failed item creates a new pending attempt', () => {
  const item = queue.add(file, null, meta);
  queue.update(item.id, { status: 'failed', jobId: 'old' });
  queue.retry(item.id);
  assert.equal(queue.get(item.id).status, 'pending');
  assert.equal(queue.get(item.id).jobId, null);
  assert.equal(queue.get(item.id).attempt, 2);
});
```

- [ ] **Step 2: Run Node tests and verify failure**

Run: `node --test tests/frontend/upload-queue.test.js tests/frontend/upload-flow.test.js`

- [ ] **Step 3: Implement queue API and batch lock**

`clearDone()` must only remove done; add failed-specific operations, updater-function support, bounded attempt summaries, and one active `startBatch()` loop.

- [ ] **Step 4: Implement capabilities-driven UI and multipart submission**

Use `FormData` for `item.file`; use JSON only for an actual desktop path. Show backend `detail.message`, current EPUB engine, and hide DOCX. Change native filters to a combined first entry.

- [ ] **Step 5: Remove or delegate the duplicate Manage ingest form**

The Manage page must open the Upload tab or call the same upload module; it must not POST `file.name`.

- [ ] **Step 6: Run frontend tests to green**

Run: `node --test tests/frontend/upload-queue.test.js tests/frontend/upload-flow.test.js`

### Task 3: V3 Document Projection and Library Service

**Files:**
- Modify: `app/index/v3/segment_projection.py`
- Modify: `app/index/v3/reader.py`
- Create: `app/library/__init__.py`
- Create: `app/library/v3_service.py`
- Modify: `app/http/routes_content.py`
- Test: `tests/pageindex_v3/test_library_projection.py`, `tests/test_http_api.py`

**Interfaces:**
- Produces: immutable `DocumentProjection`.
- Produces: `PinnedSearchView.get_document_projections(doc_uids, include_tree=False)`.
- Produces: `LibraryV3Service.list_documents()`, `read_document()`, `read_section()`.

- [ ] **Step 1: Write failing authenticated projection tests**

```python
def test_projection_returns_document_tree_and_source_fingerprint(v3_segment):
    projection = projector.load_document(ref)
    assert projection.doc_key == "paper:alpha"
    assert projection.document_tree["structure"]
    assert projection.source_files[0]["sha256"]
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python -m pytest tests/pageindex_v3/test_library_projection.py -q`

- [ ] **Step 3: Implement validated immutable projection**

Validate owner/ref/doc key, metadata types, unique nodes, nonnegative ranges, normalized source paths, and source fingerprint membership. Return deep-copied immutable data.

- [ ] **Step 4: Implement pin-keyed `LibraryV3Service` with bounded LRU**

Open the exact current view once per request, build one in-memory list snapshot per pin, and lazily load one document Segment for `/read`.

- [ ] **Step 5: Replace Legacy routes with V3-only routes**

Preserve old fields, add generation/view/doc identities, return `INDEX_NOT_READY`, `VIEW_CHANGED`, `CONTENT_OUT_OF_SYNC`, and `V3_VIEW_UNAVAILABLE` as specified. Keep the old section form for one version with a deprecation log.

- [ ] **Step 6: Run V3 and HTTP tests to green**

Run: `python -m pytest tests/pageindex_v3/test_library_projection.py tests/test_http_api.py -k "ContentApi or Library or projection" -q`

### Task 4: Library Frontend Pinning and Index-Publish Completion

**Files:**
- Modify: `frontend/library/reader.js`
- Modify: `frontend/library/session.js`
- Modify: `frontend/upload/upload.js`
- Modify: `app/ingest/pipeline.py`
- Modify: `app/index/status.py`
- Test: `tests/frontend/library-v3.test.js`, `tests/frontend/library-session.test.js`, `tests/test_ingest.py`

**Interfaces:**
- Consumes: V3 content response pin and `result.build_job_id`.
- Produces: `index:published` event after build terminal success.

- [ ] **Step 1: Write failing pin propagation and build-wait tests**

```javascript
test('read and section requests carry the shelf pin', async () => {
  await loadDocs(session);
  await selectDoc(session, 'papers', 'alpha');
  assert.match(fetches[1], /generation=/);
  assert.match(fetches[1], /view_id=/);
});
```

- [ ] **Step 2: Run Node/Python focused tests and verify failure**

Run: `node --test tests/frontend/library-v3.test.js tests/frontend/library-session.test.js`

- [ ] **Step 3: Store and propagate pin; retry `VIEW_CHANGED` once**

Session stores `generation`/`viewId`; document and section requests pass them. A 409 refreshes shelf once and never loops.

- [ ] **Step 4: Wait for the triggered V3 build before queue completion**

Pipeline result exposes `build_job_id`; upload polls `/api/index/build/{id}` and emits `index:published` only on success.

- [ ] **Step 5: Run focused tests to green**

Run: `node --test tests/frontend/library-v3.test.js tests/frontend/library-session.test.js; python -m pytest tests/test_ingest.py -q`

### Task 5: Explicit Offline Policy and Clean Isolation

**Files:**
- Modify: `app/http/schemas.py`
- Modify: `app/ingest/jobs.py`
- Modify: `app/ingest/clean_adapter.py`
- Modify: `app/ingest/translate_adapter.py`
- Modify: `app/vendor/clean_markdown.py`
- Modify: `frontend/upload/upload.js`, `frontend/config/config.js`
- Test: `tests/test_ingest_policy.py`, `tests/test_clean_pseudo_headings.py`, `tests/test_ingest.py`

**Interfaces:**
- Produces: `extract_strategy`, `network_policy` job fields.
- Produces: `clean(content, *, heading_mode="regex", classifier=None)`.

- [ ] **Step 1: Write failing offline network-sentinel tests**

```python
def test_offline_clean_never_calls_classifier(monkeypatch):
    called = 0
    def forbidden(_items):
        nonlocal called
        called += 1
        raise AssertionError("network classifier called")
    cleaned, stats = clean(TEXT, heading_mode="regex", classifier=forbidden)
    assert called == 0
    assert stats["llm_attempted"] is False
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python -m pytest tests/test_ingest_policy.py tests/test_clean_pseudo_headings.py -q`

- [ ] **Step 3: Thread network policy through schema, job, and adapters**

Reject `offline+mineru` and offline network stages before job creation. Old `strategy` remains an alias for one release; new UI always sends explicit fields.

- [ ] **Step 4: Remove implicit process-global clean behavior**

Pass the classifier per job; offline never loads LLM config. Record `classifier`, `llm_attempted`, `llm_succeeded`, and `llm_failed`.

- [ ] **Step 5: Run policy and clean tests to green**

Run: `python -m pytest tests/test_ingest_policy.py tests/test_clean_pseudo_headings.py tests/test_ingest.py -q`

### Task 6: Text Extraction and Search Normalization

**Files:**
- Create: `app/text/__init__.py`
- Create: `app/text/normalization.py`
- Modify: `app/pdf/local.py`
- Modify: `app/pdf/base.py`
- Modify: `app/retrieval/tokenizer.py`
- Test: `tests/test_text_normalization.py`, `tests/test_pdf.py`, `tests/retrieval/test_py_retrieval.py`

**Interfaces:**
- Produces: `normalize_extracted_text(text) -> tuple[str, TextStats]`.
- Produces: `normalize_for_search(text) -> str`.

- [ ] **Step 1: Write golden and idempotence tests**

```python
@pytest.mark.parametrize((raw, expected), [("efﬁciency", "efficiency"), ("L¨u", "Lü"), ("中文–数学 α", "中文–数学 α")])
def test_normalization_golden(raw, expected):
    assert normalize_extracted_text(raw)[0] == expected
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python -m pytest tests/test_text_normalization.py tests/test_pdf.py -q`

- [ ] **Step 3: Implement conservative NFC normalization and PyMuPDF flags**

Expand FB00–FB06, normalize NBSP/soft hyphen and high-confidence spacing accents. Use NFC, never global NFKC. Disable `TEXT_PRESERVE_LIGATURES` in local extraction.

- [ ] **Step 4: Normalize both indexing and query input**

Call `normalize_for_search()` at the tokenizer boundary so legacy ligatures and plain ASCII queries produce the same tokens.

- [ ] **Step 5: Run focused tests to green**

Run: `python -m pytest tests/test_text_normalization.py tests/test_pdf.py tests/retrieval/test_py_retrieval.py -q`

### Task 7: Chat Layout Hardening

**Files:**
- Modify: `frontend/core/shell.css`
- Modify: `frontend/chat/chat.css`
- Modify: `frontend/chat/index.js`
- Test: `tests/frontend/chat-layout.test.js`, `tests/test_frontend_chat.py`

**Interfaces:**
- Produces: stable chat scroll ownership and composer bounds.

- [ ] **Step 1: Write failing layout contract tests**

Assert required selectors contain `min-height: 0`, chat mount resets main-body scroll, and composer remains a nonshrinking child.

- [ ] **Step 2: Run tests and verify failure**

Run: `node --test tests/frontend/chat-layout.test.js; python -m pytest tests/test_frontend_chat.py -q`

- [ ] **Step 3: Apply minimal Flex/Grid hardening**

Add `min-height: 0` to main/body/chat/empty/messages, give empty/messages vertical scrolling, preserve composer `flex-shrink: 0`, and reset shared scroll on mount. Do not globally hide main-body overflow.

- [ ] **Step 4: Run tests to green**

Run: `node --test tests/frontend/chat-layout.test.js; python -m pytest tests/test_frontend_chat.py -q`

### Task 8: Documentation, Full Regression, Desktop Acceptance, and Final Commit

**Files:**
- Modify: `docs/architecture.md`, `docs/development.md`
- Include: `docs/ingest-e2e-test-2026-08-04.md`, `docs/ingest-remediation-technical-spec-2026-08-04.md`, this plan
- Review: all changed files

**Interfaces:**
- Consumes: all prior tasks.
- Produces: one tested commit on `dev`.

- [ ] **Step 1: Update architecture and development documentation**

Document multipart upload, capabilities, EPUB engine selection, explicit network policy, V3-only Library, queue semantics and exact test commands.

- [ ] **Step 2: Run all Python tests**

Run: `python -m pytest -q`

Expected: all tests pass; only pre-existing documented skips are allowed.

- [ ] **Step 3: Run all Node tests**

Run: `node --test tests/frontend/chat-search-api.test.js tests/frontend/library-session.test.js tests/frontend/tab-ids.test.js tests/frontend/wikilinks.test.js tests/frontend/upload-queue.test.js tests/frontend/upload-flow.test.js tests/frontend/library-v3.test.js tests/frontend/chat-layout.test.js`

Expected: all tests pass.

- [ ] **Step 4: Run isolated PDF and EPUB end-to-end tests**

Use temporary config/data roots, offline network sentinel, wait for ingest and V3 build, then verify search/list/read/section share a pin. Do not write real `data`.

- [ ] **Step 5: Run Windows pywebview desktop acceptance**

Verify native picker, drag/drop, offline import, queue retry/clear, V3 Library refresh and composer bounds at 1000×600, 1387×762 and 1400×900. Capture logs and screenshots for any failure.

- [ ] **Step 6: Review final scope**

Run: `git status --short; git diff --check; git diff --stat`

Exclude `node_modules/`, `scripts_debug/` and unrelated user files. Confirm whether the pre-existing `app/main.py` DevTools change belongs to this commit; it is expected to be included because it is related to desktop behavior and already requested previously.

- [ ] **Step 7: Commit after all gates pass**

```powershell
git add <explicit related paths>
git commit -m "fix: harden ingest and converge library on v3"
```

Do not push unless the user separately asks to push.
