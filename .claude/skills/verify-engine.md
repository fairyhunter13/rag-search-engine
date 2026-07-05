# verify-engine skill

Full engine-feature coverage loop: probe every surface → fix any RED → commit → push → repeat until 100% green.

## What this skill verifies

Every feature of rag-search-engine, end-to-end, against your registered indexed projects.

### MCP tool surfaces
- `search` — code, docs, all scopes; multiple queries; non-empty results
- `ask` — scopes: all, architecture, global, feature, business; non-empty answers
- `graph` — callers, callees, impact_narrative, semantic_trace, path; non-empty responses
- `overview` — structure, communities, patterns, service_mesh, feature_map, process_flows, business_rules, import_cycles, suggested_questions
- `index` — register/remove roundtrip

### KB question categories
Each must return a non-empty, grounded answer — not a "no data" fallback:
1. "What are the business processes in this repository?" → `ask(scope='business')` or `overview(what='process_flows')`
2. "Which code is related to checkout / key feature?" → `search` + `graph(semantic_trace)`
3. "How does service communication work?" → `ask(scope='feature')` + `graph(callers)`
4. "How does the integration between services work?" → `ask(scope='global')` + `overview(what='service_mesh')`
5. "What is the root cause of a bug?" → `ask` (debug) + `graph(impact_narrative)`
6. "How do we trace a function call?" → `graph(semantic_trace)` + `graph(callers/callees)`

### Constraints enforced in every probe
- GPU-only: embeddings + reranking via FastEmbed/ONNX/CUDA. No CPU fallback.
- KB build = cloud DeepSeek-only; dashboard chat = claude-haiku-4-5 only
- Auto-pipeline on by default: `GET /api/auto_pipeline_status` → `enabled: true`

## Loop body

```
1. RUN the fast test suite → capture all failures
2. For each failure: search() source → classify code-bug vs infra → fix root cause
   - Code bug: minimal edit → run the specific test → confirm green
   - Infra (embedding model cold start): wait for GPU warm-up → rerun, never skip
3. After fixing: run fast suite again → if clean, continue
4. RUN the slow non-browser suite → fix any failures
5. PROBE each KB question category via the MCP tools directly → assert non-empty answers
6. CHECK GPU enforcement: CUDA provider active, no CPU fallback
7. CHECK auto-pipeline: GET /api/auto_pipeline_status → enabled must be true
8. RECONCILE global prompts: run scripts/configure_integrations.py --apply-all (idempotent)
9. DASHBOARD drive: each view (Pulse/Chat/Graph/Wiki/Admin) loads; send one chat query;
   confirm SSE streams a real answer (non-empty, intent != null)
10. COMMIT + PUSH: git add source + tests; commit message; git push origin main
11. If any step failed: loop back to step 1
```

## Stopping condition

Stop when ALL of:
- Fast suite: 0 failed
- Slow suite: 0 failed
- KB question categories: all return non-empty answers
- GPU enforcement: CUDA active, no CPU fallback
- Auto-pipeline: enabled=true
- Dashboard chat: SSE streams real answer

## Model strategy

Use the `/model` setting, not hardcoded model names:

- **Planning / investigation / debugging** → enter plan mode (`/model opus` activates Opus automatically).
- **Execution / editing / committing** → exit plan mode; Sonnet handles code edits, test runs, git operations.

## What it will NOT do
- Skip failing tests
- Add mocks or fakes
- Use CPU for inference
- Amend existing commits
- Auto-index projects (only when user explicitly asks)

Run the loop now.
