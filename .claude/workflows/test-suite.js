export const meta = {
  name: 'test-suite',
  description: 'Run full opencode-search test suite: fast + slow + browser, report results',
  phases: [
    { title: 'Fast', detail: 'Run 330 fast live tests (no LLM, no browser)' },
    { title: 'Slow', detail: 'Run 281 slow LLM-heavy tests' },
    { title: 'Browser', detail: 'Run 136 Playwright/Chromium browser tests' },
    { title: 'Report', detail: 'Aggregate results and flag any failures' },
  ],
}

const FAST_CMD = '.venv/bin/pytest src/tests/live/ -m "live and not slow" -q --ignore=src/tests/live/test_browser.py --tb=short 2>&1 | tail -30'
const SLOW_CMD = '.venv/bin/pytest src/tests/live/ -m "slow" -q --ignore=src/tests/live/test_browser.py --tb=short 2>&1 | tail -40'
const BROWSER_CMD = '.venv/bin/pytest src/tests/live/test_browser.py -q --browser chromium --tb=short 2>&1 | tail -40'
const PROJ_DIR = '/home/user/git/github.com/fairyhunter13/opencode-search-engine'

const RESULT_SCHEMA = {
  type: 'object',
  properties: {
    suite: { type: 'string' },
    passed: { type: 'number' },
    failed: { type: 'number' },
    skipped: { type: 'number' },
    duration_s: { type: 'number' },
    failures: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          test: { type: 'string' },
          error: { type: 'string' },
          root_cause: { type: 'string' },
          is_infra: { type: 'boolean' },
        },
        required: ['test', 'error', 'root_cause', 'is_infra'],
      },
    },
  },
  required: ['suite', 'passed', 'failed', 'skipped', 'failures'],
}

phase('Fast')
const fastResult = await agent(
  `Run the opencode-search fast test suite. Execute this exact bash command in ${PROJ_DIR}:\n\n${FAST_CMD}\n\nParse the output and return structured results. For each failed test: extract the test name, error message, identify root cause, and whether it's an infrastructure issue (daemon down, Ollama not responding, GPU cold) vs a code bug.`,
  { label: 'fast-suite', schema: RESULT_SCHEMA }
)

phase('Slow')
const slowResult = await agent(
  `Run the opencode-search slow test suite (LLM-heavy tests). Execute this exact bash command in ${PROJ_DIR}:\n\n${SLOW_CMD}\n\nParse the output and return structured results. For each failed test: extract the test name, error message, identify root cause, and whether it's an infrastructure issue vs a code bug. Note: these tests call real Ollama LLM — timeouts up to 5 min per test are normal.`,
  { label: 'slow-suite', schema: RESULT_SCHEMA }
)

phase('Browser')
const browserResult = await agent(
  `Run the opencode-search browser test suite (Playwright/Chromium). Execute this exact bash command in ${PROJ_DIR}:\n\n${BROWSER_CMD}\n\nParse the output and return structured results. For each failed test: extract the test name, error message, identify root cause, and whether it's an infrastructure issue vs a code bug. Note: these tests drive the real dashboard at http://localhost:8765/dashboard.`,
  { label: 'browser-suite', schema: RESULT_SCHEMA }
)

phase('Report')
const results = [fastResult, slowResult, browserResult].filter(Boolean)

const totalPassed = results.reduce((s, r) => s + (r.passed || 0), 0)
const totalFailed = results.reduce((s, r) => s + (r.failed || 0), 0)
const allFailures = results.flatMap(r => r.failures || [])

log(`Total: ${totalPassed} passed, ${totalFailed} failed`)

if (totalFailed > 0) {
  log(`Failures found — classifying by type`)
  const codeBugs = allFailures.filter(f => !f.is_infra)
  const infraIssues = allFailures.filter(f => f.is_infra)
  if (infraIssues.length) log(`Infrastructure issues: ${infraIssues.map(f => f.test).join(', ')}`)
  if (codeBugs.length) log(`Code bugs to fix: ${codeBugs.map(f => f.test).join(', ')}`)
}

return {
  fast: fastResult,
  slow: slowResult,
  browser: browserResult,
  summary: {
    total_passed: totalPassed,
    total_failed: totalFailed,
    clean: totalFailed === 0,
    failures: allFailures,
  },
}
