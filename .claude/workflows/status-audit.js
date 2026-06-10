export const meta = {
  name: 'status-audit',
  description: 'Full system status audit: registry, MCP tools, dashboard, GPU, fast tests',
  phases: [
    { title: 'Probe', detail: 'Check registry, dashboard, GPU state in parallel' },
    { title: 'MCP', detail: 'Verify all 7 MCP tools against astro-project' },
    { title: 'Tests', detail: 'Run fast test suite' },
    { title: 'Summary', detail: 'Aggregate all findings into a status report' },
  ],
}

const ASTRO = '/home/user/git/github.com/fairyhunter13/astro-project'
const DAEMON = 'http://localhost:8765'

const HEALTH_SCHEMA = {
  type: 'object',
  properties: {
    check: { type: 'string' },
    ok: { type: 'boolean' },
    detail: { type: 'string' },
    issues: { type: 'array', items: { type: 'string' } },
  },
  required: ['check', 'ok', 'detail', 'issues'],
}

phase('Probe')

const [registryCheck, dashboardCheck, gpuCheck] = await parallel([
  () => agent(
    `Check the opencode-search registry health:\n1. Run: curl -s http://localhost:8765/api/projects\n2. Count: total projects, projects with communities=0, projects in /tmp or .venv paths\n3. List any stale/empty entries (path, communities, file_count)\n4. Return ok=true only if no stale entries exist`,
    { label: 'registry', schema: HEALTH_SCHEMA }
  ),
  () => agent(
    `Check opencode-search dashboard route health. Run these curl commands:\n- curl -s -o /dev/null -w "%{http_code}" http://localhost:8765/dashboard\n- curl -s -o /dev/null -w "%{http_code}" http://localhost:8765/api/projects\n- curl -s -o /dev/null -w "%{http_code}" http://localhost:8765/api/jobs\n- curl -s -o /dev/null -w "%{http_code}" http://localhost:8765/api/metrics\n- curl -s "http://localhost:8765/api/kb_health?project=${ASTRO}" | python3 -m json.tool\nReturn ok=true if all routes return expected status (200 for most, kb_health shows enrichment_pct=100.0)`,
    { label: 'dashboard', schema: HEALTH_SCHEMA }
  ),
  () => agent(
    `Check GPU and Ollama state:\n1. Run: nvidia-smi --query-gpu=temperature.gpu,memory.used,memory.total --format=csv,noheader\n2. Run: curl -s http://localhost:11434/api/ps\n3. Check if qwen3-query:8b is in the /api/ps response with size_vram > 0\n4. Return ok=true if: GPU temp < 85°C AND qwen3-query is GPU-resident (size_vram > 0)`,
    { label: 'gpu', schema: HEALTH_SCHEMA }
  ),
])

phase('MCP')

const MCP_TOOL_SCHEMA = {
  type: 'object',
  properties: {
    tool: { type: 'string' },
    ok: { type: 'boolean' },
    result_summary: { type: 'string' },
  },
  required: ['tool', 'ok', 'result_summary'],
}

const mcpResults = await parallel([
  () => agent(
    `Test the opencode-search 'search' MCP tool against astro-project.\nCall: search(query='payment service handler', project_paths=['${ASTRO}'])\nReturn ok=true if results is non-empty and contains real code snippets (not just wiki entries). Summarize what was found.`,
    { label: 'mcp-search', schema: MCP_TOOL_SCHEMA }
  ),
  () => agent(
    `Test the opencode-search 'ask' MCP tool against astro-project.\nCall: ask(query='how does authentication work', project_path='${ASTRO}', scope='feature')\nReturn ok=true if answer is non-empty and contains entry_points. Summarize the answer.`,
    { label: 'mcp-ask', schema: MCP_TOOL_SCHEMA }
  ),
  () => agent(
    `Test the opencode-search 'graph' MCP tool against astro-project.\nCall: graph(symbol='OrderService', project_path='${ASTRO}', relation='impact_narrative')\nReturn ok=true if summary field is non-empty and risk field is present. Summarize result.`,
    { label: 'mcp-graph', schema: MCP_TOOL_SCHEMA }
  ),
  () => agent(
    `Test the opencode-search 'overview' MCP tool against astro-project with multiple what= values.\nCall: overview(project_path='${ASTRO}', what='status') — confirm indexed=true and communities>5000\nCall: overview(project_path='${ASTRO}', what='architecture_domains') — confirm non-empty response\nReturn ok=true if both succeed. Summarize.`,
    { label: 'mcp-overview', schema: MCP_TOOL_SCHEMA }
  ),
])

phase('Tests')

const FAST_RESULT_SCHEMA = {
  type: 'object',
  properties: {
    passed: { type: 'number' },
    failed: { type: 'number' },
    ok: { type: 'boolean' },
    failures: { type: 'array', items: { type: 'string' } },
  },
  required: ['passed', 'failed', 'ok', 'failures'],
}

const fastTests = await agent(
  `Run the opencode-search fast test suite:\n.venv/bin/pytest src/tests/live/ -m "live and not slow" -q --ignore=src/tests/live/test_browser.py --tb=line 2>&1 | tail -20\n\nWorking directory: /home/user/git/github.com/fairyhunter13/opencode-search-engine\n\nReturn: passed count, failed count, ok=true if failed=0, and list of any failure test names.`,
  { label: 'fast-tests', schema: FAST_RESULT_SCHEMA }
)

phase('Summary')

const allChecks = [
  registryCheck, dashboardCheck, gpuCheck,
  ...(mcpResults || []),
  fastTests ? { check: 'fast-tests', ok: fastTests.ok, detail: `${fastTests.passed} passed, ${fastTests.failed} failed`, issues: fastTests.failures } : null,
].filter(Boolean)

const issues = allChecks.filter(c => !c.ok)
const clean = issues.length === 0

log(clean ? 'All checks passed' : `${issues.length} checks failed`)

return {
  clean,
  checks: allChecks,
  issues: issues.map(c => ({ check: c.check, detail: c.detail, issues: c.issues })),
  fast_tests: fastTests,
}
