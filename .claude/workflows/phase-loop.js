export const meta = {
  name: 'phase-loop',
  description: 'Autonomous phase loop: detect failures, fix code, run tests, commit, push — repeat until clean',
  phases: [
    { title: 'Detect', detail: 'Run fast tests, find failures, classify by type' },
    { title: 'Fix', detail: 'Fix code bugs in parallel (one agent per failure)' },
    { title: 'Verify', detail: 'Run fast tests again to confirm all fixes' },
    { title: 'Commit', detail: 'Commit clean codebase and push to origin/main' },
  ],
}

const PROJ = '/home/user/git/github.com/fairyhunter13/opencode-search-engine'

const FAILURES_SCHEMA = {
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
          test_file: { type: 'string' },
          error_message: { type: 'string' },
          root_cause: { type: 'string' },
          is_infra: { type: 'boolean' },
          suggested_fix: { type: 'string' },
        },
        required: ['test_name', 'test_file', 'error_message', 'root_cause', 'is_infra'],
      },
    },
  },
  required: ['passed', 'failed', 'failures'],
}

const FIX_SCHEMA = {
  type: 'object',
  properties: {
    test_name: { type: 'string' },
    fixed: { type: 'boolean' },
    files_changed: { type: 'array', items: { type: 'string' } },
    description: { type: 'string' },
  },
  required: ['test_name', 'fixed', 'files_changed', 'description'],
}

phase('Detect')

const detection = await agent(
  `Run the opencode-search fast test suite and collect all failures.\n\nCommand (run in ${PROJ}):\n.venv/bin/pytest src/tests/live/ -m "live and not slow" -q --ignore=src/tests/live/test_browser.py --tb=short 2>&1\n\nFor each failed test:\n- test_name: full pytest node id\n- test_file: path relative to project root\n- error_message: the actual error (not traceback noise)\n- root_cause: what is actually wrong\n- is_infra: true if the issue is Ollama/daemon/GPU not ready (not a code bug)\n- suggested_fix: what code change would fix it\n\nConstraints: GPU only (no CPU fallback). No mocks.`,
  { label: 'detect-failures', schema: FAILURES_SCHEMA }
)

if (!detection || detection.failed === 0) {
  log(`No failures detected — system is clean (${(detection || {}).passed || 0} passed)`)
  return { clean: true, passed: (detection || {}).passed || 0, fixed: [] }
}

log(`${detection.failed} failures detected — proceeding to fix`)

const codeBugs = detection.failures.filter(f => !f.is_infra)
const infraIssues = detection.failures.filter(f => f.is_infra)

if (infraIssues.length) {
  log(`${infraIssues.length} infrastructure issues: ${infraIssues.map(f => f.test_name).join(', ')} — skipping (restart services manually)`)
}

if (!codeBugs.length) {
  log('No code bugs to fix — only infrastructure issues')
  return { clean: false, infra_only: true, infra_issues: infraIssues }
}

phase('Fix')

const fixes = await parallel(codeBugs.map(bug => () => agent(
  `Fix this test failure in the opencode-search-engine project:\n\nTest: ${bug.test_name}\nFile: ${PROJ}/${bug.test_file}\nError: ${bug.error_message}\nRoot cause: ${bug.root_cause}\nSuggested fix: ${bug.suggested_fix || 'investigate and fix'}\n\nInstructions:\n1. Read the test file and the source file it's testing\n2. Identify the minimal code change that fixes the root cause\n3. Make the edit (use Edit tool, not Write)\n4. Run the specific test to confirm it passes: .venv/bin/pytest ${PROJ}/${bug.test_file}::${bug.test_name} -q --tb=short\n5. Report what you changed\n\nConstraints:\n- NO mocks — test must use real behavior\n- NO skipping the test\n- GPU only — no CPU fallback\n- Minimal change — fix only what broke`,
  { label: `fix:${bug.test_name.replace(/[^a-z0-9]/gi, '-').slice(0, 30)}`, schema: FIX_SCHEMA }
)))

const successfulFixes = (fixes || []).filter(Boolean).filter(f => f.fixed)
log(`${successfulFixes.length}/${codeBugs.length} fixes applied`)

phase('Verify')

const verification = await agent(
  `Run the full fast test suite to verify all fixes.\n\nCommand (run in ${PROJ}):\n.venv/bin/pytest src/tests/live/ -m "live and not slow" -q --ignore=src/tests/live/test_browser.py --tb=short 2>&1 | tail -20\n\nReturn passed/failed counts. If any tests still fail, list them.`,
  { label: 'verify', schema: FAILURES_SCHEMA }
)

if (!verification || verification.failed > 0) {
  log(`Verification failed: ${(verification || {}).failed || '?'} tests still failing`)
  return {
    clean: false,
    fixes_applied: successfulFixes,
    remaining_failures: (verification || {}).failures || [],
  }
}

log(`All tests passing: ${verification.passed} passed, 0 failed`)

phase('Commit')

const changedFiles = successfulFixes.flatMap(f => f.files_changed || [])
const commitSummary = successfulFixes.map(f => f.description).join('; ')
const phaseN = `Phase ${args && args.phase_number ? args.phase_number : 'next'}`

const commitResult = await agent(
  `Commit and push the fixed code.\n\nFiles changed:\n${changedFiles.map(f => `- ${f}`).join('\n')}\n\nSteps:\n1. cd ${PROJ}\n2. git status — confirm only expected files changed\n3. git add ${changedFiles.join(' ')}\n4. git commit -m "${phaseN}: ${commitSummary.slice(0, 80)}"\n5. git push origin main\n6. Confirm push succeeded with the commit hash\n\nDo NOT amend. Do NOT skip hooks.`,
  { label: 'commit-push' }
)

log('Committed and pushed')

return {
  clean: true,
  passed: verification.passed,
  fixes_applied: successfulFixes,
  commit: commitResult,
}
