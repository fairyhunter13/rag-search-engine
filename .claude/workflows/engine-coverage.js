export const meta = {
  name: 'engine-coverage',
  description: 'Full opencode-search engine feature coverage: probe all surfaces against redacted-name-3, verify KB questions, fix gaps, report 0-100%',
  phases: [
    { title: 'Probe', detail: 'Fan out: test suites + KB question categories + enforcement checks in parallel' },
    { title: 'Verify', detail: 'Adversarially verify each RED finding — is it a real gap or transient infra?' },
    { title: 'Fix', detail: 'Fix each confirmed code bug; reload infra for transient issues' },
    { title: 'Reconcile', detail: 'Sync global profiles, run full suite to confirm clean' },
    { title: 'Commit', detail: 'Commit source + tests, push to origin/main' },
  ],
}

const PROJ = '/home/user/git/github.com/fairyhunter13/opencode-search-engine'
const ASTRO = '/home/user/git/github.com/fairyhunter13/redacted-name-3'
const DAEMON = 'http://localhost:8765'

// ── Schemas ─────────────────────────────────────────────────────────────────

const PROBE_SCHEMA = {
  type: 'object',
  properties: {
    group: { type: 'string' },
    passed: { type: 'number' },
    failed: { type: 'number' },
    gaps: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          check: { type: 'string' },
          error: { type: 'string' },
          is_infra: { type: 'boolean' },
          fix_hint: { type: 'string' },
        },
        required: ['check', 'error', 'is_infra'],
      },
    },
  },
  required: ['group', 'passed', 'failed', 'gaps'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    check: { type: 'string' },
    is_real_gap: { type: 'boolean' },
    reason: { type: 'string' },
    fix_action: { type: 'string' },
  },
  required: ['check', 'is_real_gap', 'reason'],
}

const FIX_SCHEMA = {
  type: 'object',
  properties: {
    check: { type: 'string' },
    fixed: { type: 'boolean' },
    files_changed: { type: 'array', items: { type: 'string' } },
    description: { type: 'string' },
  },
  required: ['check', 'fixed', 'files_changed', 'description'],
}

const SUITE_SCHEMA = {
  type: 'object',
  properties: {
    passed: { type: 'number' },
    failed: { type: 'number' },
    failures: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          test_name: { type: 'string' },
          error: { type: 'string' },
          is_infra: { type: 'boolean' },
          fix_hint: { type: 'string' },
        },
        required: ['test_name', 'error', 'is_infra'],
      },
    },
  },
  required: ['passed', 'failed', 'failures'],
}

// ── Phase: Probe ─────────────────────────────────────────────────────────────

phase('Probe')

const PROBE_GROUPS = [
  {
    name: 'fast-suite',
    prompt: `Run the fast test suite and report failures.\n\nCommand (cd ${PROJ} first):\n  .venv/bin/pytest src/tests/live/ -m "live and not slow" -q --ignore=src/tests/live/test_browser.py --tb=short 2>&1 | tail -30\n\nFor each failure: check, error, is_infra (Ollama/daemon/GPU issue = true), fix_hint.\nConstraints: GPU only. No mocks.`,
    schema: PROBE_SCHEMA,
  },
  {
    name: 'slow-suite',
    prompt: `Run the slow non-browser astro test suite and report failures.\n\nCommand (cd ${PROJ} first):\n  .venv/bin/pytest src/tests/live/ -m "slow" -q --ignore=src/tests/live/test_browser.py --tb=short 2>&1 | tail -30\n\n(This takes ~30-40 min — qwen3-query:8b against real astro index.)\nFor each failure: check, error, is_infra, fix_hint.`,
    schema: PROBE_SCHEMA,
  },
  {
    name: 'kb-questions',
    prompt: `Probe the 7 "wikipedia-for-project" KB question categories against redacted-name-3.\nDaemon: ${DAEMON}\nAstro project path: ${ASTRO}\n\nFor each question, call the HTTP API and assert the answer is non-empty and grounded (not a "no data found" fallback):\n\n1. Business processes: GET ${DAEMON}/api/overview?project=${ASTRO}&what=process_flows\n   Assert: response has process_flows list or non-empty content\n2. Related code for checkout: POST ${DAEMON}/api/search body {"q":"checkout process","project":"${ASTRO}","top_k":5}\n   Assert: results array has >=1 item\n3. Feature trace (how gRPC works): POST ${DAEMON}/api/ask body {"query":"how does gRPC service communication work","project":"${ASTRO}","scope":"feature"}\n   Assert: answer non-empty (>100 chars)\n4. Service integration (order+campaign): POST ${DAEMON}/api/ask body {"query":"how does order and campaign service integration work","project":"${ASTRO}","scope":"global"}\n   Assert: answer non-empty (>100 chars)\n5. Bug root-cause: POST ${DAEMON}/api/graph body {"symbol":"AddToCart","project":"${ASTRO}","relation":"impact_narrative"}\n   Assert: response text non-empty\n6. Function call trace: POST ${DAEMON}/api/graph body {"symbol":"AddToCart","project":"${ASTRO}","relation":"callers"}\n   Assert: callers list non-empty OR response non-empty\n7. Related functions: GET ${DAEMON}/api/overview?project=${ASTRO}&what=feature_map\n   Assert: response non-empty\n\nReport each as pass/fail with the actual response snippet as evidence.`,
    schema: PROBE_SCHEMA,
  },
  {
    name: 'enforcement',
    prompt: `Check all enforcement and integration requirements.\nDaemon: ${DAEMON}\nProject: ${PROJ}\n\n1. GPU enforcement — run in subprocess:\n   cd ${PROJ}\n   OPENCODE_LLM_PROVIDER=codex .venv/bin/python -c "from opencode_search.enricher.client import create_llm_client; create_llm_client()"\n   Assert: exits non-zero (RuntimeError)\n\n2. Auto-pipeline default: GET ${DAEMON}/api/auto_pipeline_status\n   Assert: response has enabled=true\n\n3. Dashboard chat provider: check ~/.bash_aliases for OPENCODE_QUERY_LLM_PROVIDER=codex\n   Assert: line present and not commented out\n\n4. KB provider: check ~/.bash_aliases for OPENCODE_LLM_PROVIDER=ollama\n   Assert: line present and not commented out; no active export OPENCODE_LLM_PROVIDER=codex\n\n5. Global integration — for each profile:\n   ~/.claude/CLAUDE.md, ~/.codex/AGENTS.md, ~/.hermes/config.yaml, ~/.config/opencode/AGENTS.md\n   Assert: all 7 tools (search ask graph overview build federation manage) are present\n\nReport each check as pass/fail.`,
    schema: PROBE_SCHEMA,
  },
  {
    name: 'dashboard-chat',
    prompt: `Probe the dashboard chat SSE endpoint against redacted-name-3.\nEndpoint: POST ${DAEMON}/api/chat_stream\nBody: {"project":"${ASTRO}","query":"What are the main business features in this repository?"}\nHeaders: {"Accept":"text/event-stream"}\n\nUse a real HTTP client (curl or Python urllib) with timeout=90s. Collect SSE events until "done".\n\nAssert:\n- HTTP status 200\n- At least one token event received ({"type":"token","text":"..."})\n- The done event has a non-null intent field\n- The concatenated text is >100 chars (non-empty answer)\n- The model field (if present) indicates codex or ollama (never empty)\n\nReport: status, total chars, intent, model, first 200 chars of answer.`,
    schema: PROBE_SCHEMA,
  },
]

log('Probing all engine surfaces in parallel...')

const probeResults = await parallel(PROBE_GROUPS.map(g => () =>
  agent(g.prompt, { label: `probe:${g.name}`, phase: 'Probe', schema: g.schema })
    .then(r => r ? { ...r, group: g.name } : { group: g.name, passed: 0, failed: 1, gaps: [{ check: g.name, error: 'agent returned null', is_infra: true }] })
))

const allGaps = probeResults.filter(Boolean).flatMap(r => r.gaps || [])
const totalPassed = probeResults.filter(Boolean).reduce((s, r) => s + (r.passed || 0), 0)
const totalFailed = probeResults.filter(Boolean).reduce((s, r) => s + (r.failed || 0), 0)

log(`Probe complete: ${totalPassed} passed, ${totalFailed} failed, ${allGaps.length} gaps found`)

if (allGaps.length === 0) {
  log('All checks pass — engine is at 100% coverage')
  return {
    coverage_pct: 100,
    gaps: [],
    green: true,
    passed: totalPassed,
    failed: 0,
    probe_results: probeResults,
  }
}

// ── Phase: Verify (adversarial — confirm each gap is real, not transient) ────

phase('Verify')

const deduped = allGaps.filter((g, i, arr) => arr.findIndex(x => x.check === g.check) === i)
log(`Adversarially verifying ${deduped.length} unique gap(s)...`)

const verdicts = await parallel(deduped.map(gap => () =>
  agent(
    `Adversarially verify this reported gap. Your job is to REFUTE it if you can.\n\nGap: ${gap.check}\nError: ${gap.error}\nIs infra: ${gap.is_infra}\n\nSteps:\n1. Try to reproduce the failure yourself (run the same check)\n2. If the check passes now (transient infra), is_real_gap=false\n3. If the check still fails (real code bug), is_real_gap=true\n4. For infra issues (Ollama cold, model eviction): is_real_gap=false, note "reload model"\n5. For code bugs: fix_action = minimal code change description\n\nProject: ${PROJ}. GPU only. No mocks.`,
    { label: `verify:${gap.check.slice(0, 30)}`, phase: 'Verify', schema: VERDICT_SCHEMA }
  ).then(v => v ? { ...v, gap } : null)
))

const confirmed = verdicts.filter(Boolean).filter(v => v.is_real_gap)
const transient = verdicts.filter(Boolean).filter(v => !v.is_real_gap)

log(`${confirmed.length} confirmed gaps, ${transient.length} transient/infra (auto-resolved)`)

if (confirmed.length === 0) {
  log('All gaps were transient infrastructure issues — engine is at 100% after infra recovery')
  return {
    coverage_pct: 100,
    gaps: [],
    green: true,
    transient_issues: transient.map(v => v.check),
    passed: totalPassed,
  }
}

// ── Phase: Fix ───────────────────────────────────────────────────────────────

phase('Fix')

log(`Fixing ${confirmed.length} confirmed gap(s)...`)

const fixes = await parallel(confirmed.map(v => () =>
  agent(
    `Fix this confirmed engine gap.\n\nGap: ${v.check}\nError: ${v.gap.error}\nFix action: ${v.fix_action || v.reason || 'investigate and fix'}\n\nInstructions:\n1. Read source with search() first (project: ${PROJ})\n2. Identify minimal fix\n3. Apply with Edit tool\n4. Run the specific check to confirm green\n5. Report files_changed and description\n\nConstraints: NO mocks. GPU only. Minimal change.`,
    { label: `fix:${v.check.slice(0, 30)}`, phase: 'Fix', schema: FIX_SCHEMA }
  )
))

const successful = (fixes || []).filter(Boolean).filter(f => f.fixed)
log(`${successful.length}/${confirmed.length} gaps fixed`)

// ── Phase: Reconcile (full suite + global integration sync) ──────────────────

phase('Reconcile')

log('Running full fast suite after fixes + reconciling global integration...')

const reconcile = await parallel([
  () => agent(
    `Reconcile global integration profiles (idempotent).\n\nRun: cd ${PROJ} && python scripts/configure_integrations.py --apply-all 2>&1 | tail -20\n\nReport: what was updated (expected to be no-op if already in sync). Then verify each of the 4 profiles still has all 7 tools (search ask graph overview build federation manage).\n\nDo NOT change any test file. Just run the script and confirm.`,
    { label: 'reconcile-profiles', phase: 'Reconcile' }
  ),
  () => agent(
    `Run the full fast test suite to confirm all fixes landed cleanly.\n\nCommand (cd ${PROJ} first):\n  .venv/bin/pytest src/tests/live/ -m "live and not slow" -q --ignore=src/tests/live/test_browser.py --tb=short 2>&1 | tail -20\n\nReturn passed/failed counts and any remaining failures.`,
    { label: 'final-fast-suite', phase: 'Reconcile', schema: SUITE_SCHEMA }
  ),
])

const finalSuite = reconcile[1]
const remainingFailed = finalSuite ? finalSuite.failed : -1

if (remainingFailed > 0) {
  log(`WARNING: ${remainingFailed} tests still failing after fixes — loop would continue`)
}

const coveragePct = Math.round((1 - (remainingFailed > 0 ? remainingFailed : 0) / Math.max(totalFailed, 1)) * 100)

// ── Phase: Commit ────────────────────────────────────────────────────────────

if (successful.length > 0 && remainingFailed === 0) {
  phase('Commit')

  const changedFiles = successful.flatMap(f => f.files_changed || [])
  const summary = successful.map(f => f.description).join('; ')

  await agent(
    `Commit and push the engine-coverage fixes.\n\nFiles changed:\n${changedFiles.map(f => `- ${f}`).join('\n')}\n\nSteps:\n1. cd ${PROJ}\n2. git status — confirm only expected files\n3. git add ${changedFiles.join(' ')}\n4. git commit -m "feat: engine-coverage fixes — ${summary.slice(0, 80)}"\n5. git push origin main\n6. Confirm push with commit hash\n\nDo NOT amend. Do NOT skip hooks.`,
    { label: 'commit-push', phase: 'Commit' }
  )

  log('Committed and pushed engine-coverage fixes')
}

return {
  coverage_pct: coveragePct,
  gaps: confirmed.map(v => v.check),
  green: remainingFailed === 0 && confirmed.filter(v => !successful.find(f => f.check === v.check)).length === 0,
  passed: finalSuite ? finalSuite.passed : totalPassed,
  failed: remainingFailed,
  fixes_applied: successful,
  transient_issues: transient.map(v => v.check),
  probe_results: probeResults.map(r => ({ group: r.group, passed: r.passed, failed: r.failed })),
}
