export const meta = {
  name: 'codex-review-20-features',
  description: 'Review the 20-feature Codex-style changes for bugs, interaction consistency, and quality',
  phases: [
    { title: 'Find', detail: 'dimensional review of changed files' },
    { title: 'Verify', detail: 'adversarial verification of each finding' },
  ],
}

const CHANGED_FILES = args.changedFiles
const REPO = args.repo

const FINDINGS_SCHEMA = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          title: { type: 'string' },
          file: { type: 'string' },
          line: { type: 'integer' },
          severity: { type: 'string', enum: ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'] },
          description: { type: 'string' },
          failure: { type: 'string' },
        },
        required: ['title', 'file', 'line', 'severity', 'description', 'failure'],
      },
    },
  },
  required: ['findings'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    isReal: { type: 'boolean' },
    reason: { type: 'string' },
  },
  required: ['isReal', 'reason'],
}

function fileList() {
  return CHANGED_FILES.map(function (f) { return '- ' + f; }).join('\n')
}

const COMMON_CONTEXT =
  'You are reviewing commit ' + args.commit + ' in the LQ-D desktop app at ' + REPO +
  ' (Windows, FastAPI backend + vanilla JS frontend, pywebview desktop shell). The commit added 20 Codex-style features:\n' +
  '- P0: stop-generation button (AbortController), message hover actions (copy/regenerate), code-block copy button, multi-session tabs (chat no longer singleton), smart session titles\n' +
  '- P1: retrieval scope toggle (backend doc_types filter + UI), follow-up awareness (pronoun expansion), citation copy-source + open-node, low-confidence follow-up chips, search-failure recovery guidance\n' +
  '- P2: session search/grouping, export-to-markdown, rename\n' +
  '- P3: slash commands (/new /clear /export /scope /status /help), keyboard nav, Ctrl+Shift+F global search, drag-drop upload to chat, build-progress toasts, one-click /status diagnostics\n' +
  '- Animation polish: nav hover feedback, global focus-visible ring, --ease-fluid consistency\n\n' +
  'READ the actual changed files in ' + REPO + ' (use Read/Grep tools). The files are:\n' + fileList() + '\n\n' +
  'Find REAL bugs, interaction inconsistencies, or quality issues. For each finding give: title, file, approximate line, severity (CRITICAL=breaks feature/user-facing crash, HIGH=significant bug, MEDIUM=maintainability/interaction issue, LOW=style), a description, and the concrete failure scenario (inputs/state -> wrong behavior/crash). Be specific and reference actual code. Skip style nits. Return ONLY real, actionable findings - quality over quantity.'

const DIMENSIONS = [
  {
    key: 'correctness',
    prompt: COMMON_CONTEXT + '\n\nDIMENSION: Correctness/bugs. Focus on: the AbortController/stop flow (does abort properly unwind all loops/streams? is partial text saved? do toasts fire once?), the regenerate flow (does it correctly find the preceding user message and truncate session?), multi-session tab isolation (do sessions/refs stay per-tab? does the singleton removal break restore/archive?), the doc_types backend filter (does it normalize types correctly for both V3 and legacy paths?), the follow-up pronoun expansion (could it corrupt queries?), slash-command menu edge cases, the scope toggle at-least-one guard. Trace the actual code paths.',
  },
  {
    key: 'interaction',
    prompt: COMMON_CONTEXT + '\n\nDIMENSION: Interaction/UX consistency. Focus on: does the stop button actually show during generation and hide after? Do hover action bars overlap with citations or the composer? Is the scope panel / slash menu positioned correctly and dismissed on outside click / Escape? Does the follow-up chip click reliably populate the composer? Do session search results re-render without losing the input focus? Keyboard nav: do Arrow keys/Enter work when focus is in the sidebar? Is the drag-drop highlight (lqd-chat-dragover) removed correctly? Any element that could overlap, trap focus, or behave inconsistently with the existing Codex-style UI?',
  },
  {
    key: 'frontend-quality',
    prompt: COMMON_CONTEXT + '\n\nDIMENSION: Frontend JS quality / robustness. Focus on: duplicated event listeners (e.g. multiple input keydown handlers in composer), unhandled null derefs (e.g. scopeBtn/scopePanel being null, stopBtn missing in older refs), the base64 copy encoding edge cases (unicode, very large code), the exportSessionMarkdown/downloadTextFile blob handling, global document click delegation (the codebox copy handler uses capture=true - could it double-fire or break other clicks?), localStorage quota/serialization of scope, and whether new functions are properly exposed on their window.Lqd* namespaces before consumers run.',
  },
  {
    key: 'regression',
    prompt: COMMON_CONTEXT + '\n\nDIMENSION: Regression risk against existing behavior. Focus on: did removing chat from SINGLETON_TYPES break LqdTabPersistence restore or openNewChat reuse logic? Did adding the search box to renderSidebar break the empty-state or the delete-confirm re-render path? Did the message hover actions change appendMessageBubble return contract (index.js and agent.js rely on it returning the content element)? Does the scope filter accidentally exclude results when scope is all-default? Did the composer refactor (adding stopBtn/scopeBtn) break the existing return object shape that agent.js/index.js destructure? Run through the callers of every changed function signature.',
  },
]

phase('Find')
const findingsByDim = await parallel(
  DIMENSIONS.map(function (d) {
    return function () {
      return agent(d.prompt, { label: 'review:' + d.key, phase: 'Find', schema: FINDINGS_SCHEMA })
        .then(function (r) { return { dim: d.key, findings: (r && r.findings) || [] }; })
    }
  })
)

const all = findingsByDim.filter(Boolean).flatMap(function (x) {
  return (x.findings || []).map(function (f) { return Object.assign({}, f, { dim: x.dim }); })
})
log('Found ' + all.length + ' candidate findings')

const seen = new Set()
const deduped = all.filter(function (f) {
  const k = f.title + '|' + f.file
  if (seen.has(k)) return false
  seen.add(k)
  return true
})

phase('Verify')
const verified = await parallel(
  deduped.map(function (f) {
    return function () {
      return agent(
        'You are an adversarial verifier for a code review finding. The finding is about commit ' + args.commit + ' in ' + REPO + '. REPO: ' + REPO + '\n\n' +
        'FINDING:\n- Title: ' + f.title + '\n- File: ' + f.file + ' (line ~' + f.line + ')\n- Severity: ' + f.severity + '\n- Description: ' + f.description + '\n- Failure scenario: ' + f.failure + '\n\n' +
        'YOUR JOB: READ the actual code in ' + REPO + '/' + f.file + ' and try to REFUTE this finding. Is it a REAL bug, or is it wrong/outdated/already-handled? Default to isReal=false if uncertain or if the finding is minor/style-only. A finding is real only if you can trace a concrete failure from the actual current code. Return {isReal, reason}.',
        { label: 'verify:' + f.file.split('/').pop(), phase: 'Verify', schema: VERDICT_SCHEMA }
      ).then(function (v) { return Object.assign({}, f, { verdict: v }); })
    }
  })
)

const real = verified.filter(Boolean).filter(function (x) { return x.verdict && x.verdict.isReal; })
log('Confirmed ' + real.length + ' real findings')

const bySeverity = {}
real.forEach(function (f) { bySeverity[f.severity] = (bySeverity[f.severity] || 0) + 1 })
log('By severity: ' + JSON.stringify(bySeverity))

return {
  totalCandidates: all.length,
  confirmed: real.length,
  bySeverity: bySeverity,
  findings: real.map(function (f) {
    return {
      severity: f.severity,
      file: f.file,
      line: f.line,
      title: f.title,
      description: f.description,
      failure: f.failure,
      dim: f.dim,
      verifyReason: (f.verdict && f.verdict.reason) || '',
    }
  }),
  refuted: verified.filter(Boolean).filter(function (x) { return x.verdict && !x.verdict.isReal; }).length,
}
