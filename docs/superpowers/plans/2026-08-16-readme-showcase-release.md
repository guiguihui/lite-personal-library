# README Showcase and Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the real desktop application, add truthful screenshots and a short product demo to the repository README, and synchronize the GitHub About and first Release information.

**Architecture:** Capture media from the existing PyWebView desktop client without changing product behavior. Store public-safe media under `docs/assets/`, reference those assets from a user-oriented README, then publish only the documentation/media changes to the default branch before creating the Release so its tag includes the documentation update.

**Tech Stack:** Python desktop application (PyWebView + FastAPI), Markdown, PNG, animated GIF/MP4, Git, GitHub.

## Global Constraints

- Preserve all unrelated local and uncommitted work.
- Do not expose API keys, private filesystem paths, or sensitive library content in public media.
- Document only behavior verified against the current application and repository.
- Keep the README usable for a first-time Windows user.
- Publish the Release only after the README/media commit is present on `main`.

---

## Task 1: Audit the current repository and release state

- [x] Inspect the local branch, remotes, modified/untracked files, and recent commits.
- [x] Read `README.md`, `pyproject.toml`, launch scripts, architecture documentation, and ingestion configuration.
- [x] Inspect the GitHub default branch, About metadata, tags, and Releases.
- [x] Select a publishing path that does not include unrelated working-tree changes.

## Task 2: Run and capture the real desktop application

**Files:**
- Create: `docs/assets/screenshots/desktop-home.jpg`
- Create: `docs/assets/screenshots/library.jpg`
- Create: `docs/assets/screenshots/import.jpg`
- Create: `docs/assets/screenshots/index-management.jpg`
- Create: `docs/assets/demo/lq-d-demo.gif`
- Create: `docs/assets/demo/lq-d-demo.mp4`

- [x] Start `python -m app.main` from the project root.
- [x] Verify the loopback service and PyWebView desktop window both load.
- [x] Capture public-safe screenshots of the main workflow.
- [x] Produce a short demo sequence that shows navigation without exposing secrets.

## Task 3: Rewrite the README for first-time users

**Files:**
- Modify: `README.md`

- [x] Add a concise product statement and visual demo above the fold.
- [x] Add a feature overview grounded in current behavior.
- [x] Add prerequisites, installation, startup, and first-use steps.
- [x] Explain PDF/EPUB import requirements and known limitations.
- [x] Add the current architecture summary and link to `docs/architecture.md`.
- [x] Add data/privacy notes, troubleshooting, and Release links.

## Task 4: Validate the documentation package

- [x] Confirm every README media link resolves to a tracked file.
- [x] Confirm JPEG dimensions and that GIF/MP4 files open successfully.
- [x] Run the relevant application smoke test and repository tests.
- [x] Run whitespace/diff validation and inspect the final scoped diff.

## Task 5: Publish GitHub metadata and Release

- [ ] Commit only the README, plan, and media assets.
- [ ] Push and integrate the documentation update into `main`.
- [ ] Update the repository description and topics in GitHub About.
- [ ] Create the first versioned GitHub Release from the updated `main` commit.
- [ ] Verify the public README, About metadata, and Release page.
