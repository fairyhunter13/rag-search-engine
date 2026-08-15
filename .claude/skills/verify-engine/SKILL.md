---
name: verify-engine
description: "Full engine-feature coverage loop: probe every surface, fix any RED, then commit and push until green."
---

# verify-engine skill

Full engine-feature coverage loop: probe every surface → fix any RED → commit → push → repeat until 100% green.

## What this skill verifies

Every feature of rag-search-engine, end-to-end, against your registered indexed projects.

### MCP tool surfaces
- `search` — code, docs, all scopes; multiple queries; non-empty results
- `graph` — definition, callers, callees, impact, impact_narrative, path; non-empty responses.
  `semantic_trace` was deleted 2026-07-28: the implementation went in 3fe4b29 and only the name
  survived, silently answering with `path` semantics
- `overview` — structure, projects, status, metrics, communities, import_cycles, surprising_connections, validate. The five generative variants (patterns, service_mesh, feature_map, process_flows, business_rules) were deleted with tier 3 on 2026-07-28; `suggested_questions` followed on 07-29 with the chat box it seeded
- `ask` was the fifth tool and was retired 2026-07-29. It assembled a prose context blob, which is
  the shape a client that cannot loop needs — MCP clients can loop, so they get ranked locations
  from `search` and the architecture axis from `overview(what="communities", query=…)`, which now
  carries community summaries and reranks against the query. `query/ask.py` itself survives as the
  CLI's and the dashboard chat's context builder
- `index` — register/remove roundtrip

### Retrieval question categories
Each must return a non-empty, grounded answer — not a "no data" fallback. Rewritten
2026-07-28: the two questions that routed through `overview`'s `process_flows` and `service_mesh`
whats had no surface left to answer them once tier 3 was deleted, so they now ask the same thing of
the deterministic graph, which is what survived. (Named without their call parentheses on purpose —
E8c scans raw lines, so writing a retired call out in full trips it as readily as instructing one
would. Same reason `test_query.py`'s docstring phrases around its own banned vocabulary.)
1. "Which code is related to checkout / key feature?" → `search` + `graph(callers)`
2. "How does service communication work?" → `search(scope='all')` + `graph(callers)`
3. "What is the root cause of a bug?" → `search` + `graph(impact_narrative)`
4. "How do we trace a function call?" → `graph(path, --to-symbol)` + `graph(callers/callees)`
5. "What are the main modules?" → `overview(what='communities')` — structural labels, no narration
6. "Where does this project import cycles?" → `overview(what='import_cycles')`

### Constraints enforced in every probe
- GPU-only: embeddings + reranking via FastEmbed/ONNX/CUDA. No CPU fallback.
- No generative LLM anywhere except dashboard chat, which is claude-haiku-4-5 via `claude -p`
- Auto-pipeline on by default: `GET /api/auto_pipeline_status` → `enabled: true`

## Loop body

```
1. RUN the fast test suite → capture all failures
2. For each failure: search() source → classify code-bug vs infra → fix root cause
   - Code bug: minimal edit → run the specific test → confirm green
   - Infra (embedding model cold start): wait for GPU warm-up → rerun, never skip
3. After fixing: run fast suite again → if clean, continue
4. RUN the slow non-browser suite → fix any failures
5. PROBE each retrieval question category via the MCP tools directly → assert non-empty answers
6. CHECK GPU enforcement: CUDA provider active, no CPU fallback
7. CHECK auto-pipeline: GET /api/auto_pipeline_status → enabled must be true
8. RECONCILE global prompts: run scripts/configure_integrations.py --apply-all (idempotent)
9. DASHBOARD drive: each view (Pulse/Chat/Admin/Graph) loads — Wiki left with tier 3, Docs and
   Hierarchy with the operator-console pass; send one chat query;
   confirm SSE streams a real answer (non-empty, intent != null)
10. COMMIT + PUSH: git add source + tests; commit message; git push origin main
11. If any step failed: loop back to step 1
```

## Stopping condition

Stop when ALL of:
- Fast suite: 0 failed
- Slow suite: 0 failed
- Retrieval question categories: all return non-empty answers
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
