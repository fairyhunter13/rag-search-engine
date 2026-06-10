# verify-engine skill

Full engine-feature coverage loop against redacted-name-3: probe every surface → fix any RED → commit → push → repeat until 100% green.

## What this skill verifies

Every feature of opencode-search-engine, end-to-end, against the real redacted-name-3 index:

### MCP tool surfaces
- `search` — code, docs, all scopes; multiple queries; non-empty results
- `ask` — scopes: all, architecture, global, feature, business; non-empty answers
- `graph` — callers, callees, impact_narrative, semantic_trace, path; non-empty responses
- `overview` — structure, communities, patterns, service_mesh, architecture_domains, feature_map, process_flows, business_rules, import_cycles, suggested_questions
- `build` — jobs API works; enrich starts job
- `federation` — list returns; add/list/remove roundtrip
- `manage` — wiki_lint, dedup dry_run, vacuum dry_run

### KB "wikipedia" question categories (the user's central requirement)
Each must return a non-empty, grounded answer — not a "no data" fallback:
1. "What are the business processes in this repository?" → `ask(scope='business')` or `overview(what='process_flows')`
2. "Which code is related to checkout?" → `search` + `graph(semantic_trace)`
3. "How does gRPC service communication work?" → `ask(scope='feature')` + `graph(callers)`
4. "How does the integration between order and campaign services work?" → `ask(scope='global')` + `overview(what='service_mesh')`
5. "What is the real root cause of a bug?" → `ask` (debug) + `graph(impact_narrative)`
6. "How do we trace a function call through the codebase?" → `graph(semantic_trace)` + `graph(callers/callees)`
7. "Which functions are related to AddToCart?" → `graph(callers)` + `graph(impact_narrative)`

### Constraints enforced in every probe
- GPU-only: KB build uses ollama qwen3-enrich:1.7b + qwen3-query:8b (never CPU, never cloud)
- `create_llm_client()` must reject codex/claude-code with RuntimeError
- Dashboard chat = codex gpt-5.4-mini → haiku-4.5 fallback (the ONLY cloud exception)
- Auto-pipeline on by default: `GET /api/auto_pipeline_status` → `enabled: true`
- Global integration: all 4 profiles (CLAUDE.md, codex, hermes, opencode) × 7 tools

## Loop body

```
1. RUN the fast test suite → capture all failures
2. For each failure: search() source → classify code-bug vs infra → fix root cause
   - Code bug: minimal edit → run the specific test → confirm green
   - Infra (Ollama cold, model eviction): reload qwen3-query:8b → rerun, never skip
3. After fixing: run fast suite again → if clean, continue
4. RUN the slow non-browser suite (145 acme-scenario tests) → fix any failures
5. PROBE each KB question category via the MCP tools directly → assert non-empty answers
6. CHECK GPU enforcement: subprocess `create_llm_client()` with provider=codex → must raise
7. CHECK auto-pipeline: GET /api/auto_pipeline_status → enabled must be true
8. CHECK global integration: each profile contains all 7 tool names
9. RECONCILE global prompts: run scripts/configure_integrations.py --apply-all (idempotent)
10. DASHBOARD drive: each view (Pulse/Chat/Graph/Wiki/Admin) loads; send one astro chat query;
    confirm SSE streams a real answer (non-empty, intent != null)
11. COMMIT + PUSH: git add source + tests; commit "Phase N: <description>"; git push origin main
12. If any step failed: loop back to step 1
```

## Stopping condition

Stop when ALL of:
- Fast suite: 0 failed
- Slow suite: 0 failed
- KB question categories: all return non-empty answers
- GPU enforcement: codex/claude-code raise RuntimeError from create_llm_client()
- Auto-pipeline: enabled=true
- Global integration: 7/7 tools in all 4 profiles
- Dashboard chat: SSE streams real astro answer

## Model strategy

Do **not** hardcode model selection here or in `engine-coverage.js`. Instead, use the `/model` setting:

- **Planning / investigation / debugging** → enter plan mode first (`/model opusplan` activates Opus automatically when you enter plan mode). Use plan mode to investigate failures, root-cause bugs, and design fixes.
- **Execution / editing / committing** → exit plan mode; the session reverts to Sonnet for code edits, test runs, git operations.

The workflow script (`engine-coverage.js`) agents omit `opts.model` — each inherits the resolved session model. This is correct and intentional: agent model selection follows the session, which follows `/model`.

## What it will NOT do
- Skip failing tests
- Add mocks or fakes
- Use CPU for inference (except dashboard chat codex/haiku)
- Amend existing commits
- Auto-index projects (only build if user explicitly asks)
- Hardcode `{model: 'opus'}` or `{model: 'sonnet'}` — let `/model` handle it

## Output per iteration

```
=== ENGINE COVERAGE LOOP ===
Fast suite:   330 passed / 0 failed ✓
Slow suite:   145 passed / 0 failed ✓
KB Q1 business processes: 847 chars ✓
KB Q2 checkout code: found 12 related symbols ✓
...
GPU enforcement: create_llm_client(codex) → RuntimeError ✓
Auto-pipeline: enabled=true ✓
Global integration: 4/4 profiles × 7/7 tools ✓
Dashboard chat: codex → "astro uses gRPC..." (2341 chars, intent=architecture) ✓
Committed: abc1234 "Phase 94: ..."
Pushed to origin/main ✓
=== COVERAGE: 100% — 0 gaps ===
```

Run the loop now.
