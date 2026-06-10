export const meta = {
  name: 'e2e-verify',
  description: 'End-to-end verification of all 7 MCP tool surfaces against a target project',
  phases: [
    { title: 'Probe', detail: 'Fan out 7 agents — one per MCP tool surface' },
    { title: 'Verify', detail: 'Adversarial check: each finding independently verified' },
    { title: 'Report', detail: 'Synthesize results into pass/fail surface matrix' },
  ],
}

const TARGET = (args && args.project_path) || '/home/user/git/github.com/fairyhunter13/redacted-name-3'
const PROJECT_NAME = TARGET.split('/').pop()

const SURFACE_SCHEMA = {
  type: 'object',
  properties: {
    surface: { type: 'string' },
    pass: { type: 'boolean' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          check: { type: 'string' },
          passed: { type: 'boolean' },
          detail: { type: 'string' },
        },
        required: ['check', 'passed', 'detail'],
      },
    },
    error: { type: 'string' },
  },
  required: ['surface', 'pass', 'findings'],
}

const VERIFY_SCHEMA = {
  type: 'object',
  properties: {
    surface: { type: 'string' },
    confirmed: { type: 'boolean' },
    reason: { type: 'string' },
  },
  required: ['surface', 'confirmed', 'reason'],
}

const SURFACES = [
  {
    key: 'search',
    prompt: `You are testing the opencode-search MCP server's "search" tool surface against project at ${TARGET}.
Run at least 3 distinct search queries covering: (1) a function name, (2) a concept like "route handler", (3) a business term like "authentication".
For EACH query, verify: results count >= 2, results contain valid file paths, results are relevant to the query.
Use the mcp__opencode-search__search tool directly. Return surface="search", pass=true only if all 3 queries return relevant results.`,
  },
  {
    key: 'ask_all',
    prompt: `You are testing the opencode-search MCP server's "ask" tool surface (scope=all) against project at ${TARGET}.
Run 2 "ask" queries with scope="all": (1) "how does the project structure work?", (2) "what are the main components?".
For each: verify the response references real file paths from ${TARGET}, is > 100 chars, and is not a generic fallback.
Use the mcp__opencode-search__ask tool directly. Return surface="ask_all", pass=true only if both responses are grounded.`,
  },
  {
    key: 'ask_global',
    prompt: `You are testing the opencode-search MCP server's "ask" tool surface (scope=global, GraphRAG synthesis) against project at ${TARGET}.
Call ask("describe the overall architecture", project_path="${TARGET}", scope="global").
Verify: response is > 200 chars, references multiple architectural domains, not a timeout/error.
Also call ask("what are the key design decisions?", project_path="${TARGET}", scope="global") and verify it returns substantive synthesis.
Use the mcp__opencode-search__ask tool directly. Return surface="ask_global", pass=true only if both calls succeed with substantive responses.`,
  },
  {
    key: 'graph',
    prompt: `You are testing the opencode-search MCP server's "graph" tool surface against project at ${TARGET}.
First call search("main function entry point", project_paths=["${TARGET}"]) to find a real symbol name.
Then call graph(symbol=<found_symbol>, project_path="${TARGET}", relation="callees") and verify: result has symbols, not "not found".
Also call graph(symbol=<found_symbol>, project_path="${TARGET}", relation="impact_narrative") and verify: returns a narrative string.
Use the mcp__opencode-search__search and mcp__opencode-search__graph tools directly. Return surface="graph", pass=true only if both graph calls return data.`,
  },
  {
    key: 'overview',
    prompt: `You are testing the opencode-search MCP server's "overview" tool surface against project at ${TARGET}.
Run these overview calls:
1. overview(project_path="${TARGET}", what="status") — verify: files > 1000, communities > 10
2. overview(project_path="${TARGET}", what="communities") — verify: returns list with enriched titles
3. overview(project_path="${TARGET}", what="architecture_domains") — verify: returns ≥2 named domains or reports "hierarchy not built"
4. overview(project_path="${TARGET}", what="import_cycles") — verify: returns result (even if empty)
Use the mcp__opencode-search__overview tool directly. Return surface="overview", pass=true only if all 4 calls return non-error results.`,
  },
  {
    key: 'manage',
    prompt: `You are testing the opencode-search MCP server's "manage" tool surface against project at ${TARGET}.
Run manage(project_path="${TARGET}", action="wiki_lint") and verify it returns a result (pass or issues list, not an error).
Run manage(project_path="${TARGET}", action="vacuum", dry_run=True) and verify it returns a dry-run report (MB freed estimate or "nothing to vacuum").
Do NOT run vacuum without dry_run=True — this is a read-only verification pass.
Use the mcp__opencode-search__manage tool directly. Return surface="manage", pass=true only if both calls return structured results.`,
  },
  {
    key: 'federation',
    prompt: `You are testing the opencode-search MCP server's "federation" tool surface.
Call federation(root_path="/home/user/git/github.com/fairyhunter13") to list sub-repositories.
Verify: result is a list (even if empty), no error, and returns within 10s.
Then call overview(what="projects") using the mcp__opencode-search__overview tool to list all registered projects.
Verify: redacted-name-3 appears in the registry, opencode-search-engine appears.
Use the mcp__opencode-search__federation and mcp__opencode-search__overview tools directly. Return surface="federation", pass=true only if both calls succeed.`,
  },
]

// Phase 1: Probe all 7 surfaces in parallel via pipeline
phase('Probe')
log(`Probing all 7 MCP surfaces against ${PROJECT_NAME}...`)

const probeResults = await pipeline(
  SURFACES,
  async (surface) => {
    const result = await agent(surface.prompt, {
      label: `probe:${surface.key}`,
      phase: 'Probe',
      schema: SURFACE_SCHEMA,
    })
    return result
  }
)

const valid = probeResults.filter(Boolean)
log(`Probe complete: ${valid.length}/${SURFACES.length} surfaces responded`)

// Phase 2: Adversarially verify each failing surface
phase('Verify')
const failures = valid.filter(r => !r.pass)
log(`Verifying ${failures.length} failing surface(s) with independent agents...`)

const verifications = failures.length > 0
  ? await pipeline(
      failures,
      async (result) => {
        const v = await agent(
          `You are an independent verifier. Another agent reported that the "${result.surface}" MCP tool surface FAILED.
Their findings: ${JSON.stringify(result.findings, null, 2)}
${result.error ? 'Error: ' + result.error : ''}

Your job: reproduce the failure by calling the same MCP tools against ${TARGET}.
Try each failing check yourself. Report: is the failure real, or was the first agent wrong?
Be skeptical — the first agent may have used the wrong parameters or misread the output.`,
          {
            label: `verify:${result.surface}`,
            phase: 'Verify',
            schema: VERIFY_SCHEMA,
          }
        )
        return { original: result, verification: v }
      }
    )
  : []

// Phase 3: Synthesize report
phase('Report')

const confirmedFailures = verifications
  .filter(v => v && v.verification && v.verification.confirmed)
  .map(v => v.original)

const falseAlarms = verifications
  .filter(v => v && v.verification && !v.verification.confirmed)
  .map(v => ({ surface: v.original.surface, reason: v.verification.reason }))

const passed = valid.filter(r => r.pass)
const totalPass = passed.length + falseAlarms.length
const totalFail = confirmedFailures.length

const report = await agent(
  `Synthesize this E2E verification report into a clear markdown summary.

Target project: ${TARGET}
Surfaces tested: ${SURFACES.length}
Passed: ${totalPass}/${SURFACES.length}
Confirmed failures: ${totalFail}

Passing surfaces: ${JSON.stringify(passed.map(r => r.surface))}
False alarms (originally failed but independently verified as OK): ${JSON.stringify(falseAlarms)}
Confirmed failures (independently verified as broken): ${JSON.stringify(confirmedFailures.map(r => ({
    surface: r.surface,
    findings: r.findings.filter(f => !f.passed),
  })))}
Unverified surfaces (agent errors): ${SURFACES.length - valid.length}

Write a concise report with:
1. Overall verdict: PASS or FAIL with counts
2. Surface matrix table: surface | status | notes
3. For each confirmed failure: specific error and remediation command
4. For each false alarm: what the first agent got wrong

Keep it under 40 lines. Be direct and actionable.`,
  { label: 'report', phase: 'Report' }
)

return {
  project: TARGET,
  surfaces_tested: SURFACES.length,
  pass_count: totalPass,
  fail_count: totalFail,
  confirmed_failures: confirmedFailures.map(r => r.surface),
  false_alarms: falseAlarms.map(v => v.surface),
  report,
}
