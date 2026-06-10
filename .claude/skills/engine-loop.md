# engine-loop skill

Autonomous full-coverage loop: probe every search-engine surface against redacted-name-3 → fix any RED → commit + push → re-arm → repeat until 100% green.

## June 2026 loop features in use

- **ScheduleWakeup** dynamic `/loop`: re-arms with cache-window-aware delays (270s while watching in-flight KB sweep; 1200s for idle ticks)
- **SessionStart + Stop + PostCompact hooks** (from `.claude/settings.json`): inject branch/daemon/GPU/unpushed context on every wakeup — the loop is always cold-start-aware
- **fallbackModel + effortLevel**: session settings, not hardcoded here; `/model opus` activates Opus when investigating failures
- No `Workflow`, no `CronCreate`, no `workflows/` scripts

## What this loop verifies

### 1. Test suites
- Fast live suite: `.venv/bin/pytest src/tests/live/ -m "live and not slow" -q`
- Slow non-browser suite: `.venv/bin/pytest src/tests/live/ --ignore=src/tests/live/test_browser.py -q`

### 2. KB completeness (the core requirement — "wikipedia for a project")
All levels checked via `GET /api/kb_health?project=.../redacted-name-3`:
- Level-1: must be ≥ 99% enriched
- Level-2: must be > 30% enriched (rising toward 100% via periodic sweep)
- Level-3: must be > 0% enriched (sweep convergence)
- `wiki_page_count > 0`
- `patterns_cached = true`

### 3. The 7 KB question categories (must return non-empty grounded answers)
1. "What are the business processes in astro?" → `ask(scope='business')` + `overview(what='process_flows')`
2. "Which code is related to the checkout flow?" → `search` + `graph(semantic_trace)`
3. "How does gRPC service communication work?" → `ask(scope='feature')` + `graph(callers)`
4. "How does the integration between order and campaign work?" → `ask(scope='global')` + `overview(what='service_mesh')`
5. "What is the real root cause of a panic in handler X?" → `ask` debug + `graph(impact_narrative)`
6. "Trace AddToCart through the call chain" → `graph(semantic_trace)` + `graph(callers/callees)`
7. "Which functions are related to AddToCart?" → `graph(callers)` + `graph(impact_narrative)`

### 4. Invariant assertions
- **GPU-only**: `create_llm_client(provider='codex')` must raise `RuntimeError` for build path
- **Auto-pipeline**: `GET /api/auto_pipeline_status` → `enabled: true`
- **KB sweep**: daemon log contains `kb_sweep: monitor started` (or `OPENCODE_KB_SWEEP_ENABLED=1`)
- **Global integration**: `scripts/configure_integrations.py --apply-all` → 7/7 profiles OK (bash_aliases + codex + hermes + 3 claude + opencode)
- **Dashboard chat**: `POST /api/chat_stream` with an astro query → SSE streams a real non-empty answer (intent ≠ null)

## Loop body

```
while stopping conditions not met:
    1. Run fast suite → collect failures
    2. For each failure:
       a. search() source code first (mandatory — MCP before bash/read)
       b. Classify: code-bug vs infra (cold Ollama model → reload qwen3-query:8b)
       c. Minimal fix → rerun that test → confirm green
       d. Never skip, never mock, never CPU fallback
    3. Run slow non-browser suite → fix any RED (same rules)
    4. Check KB completeness via kb_health → if any level below target,
       trigger handle_enrich_hierarchy via MCP build(action='enrich_hierarchy') or
       POST /api/build (project=astro, action=enrich_hierarchy); wait and re-poll
    5. Probe all 7 KB question categories → assert non-empty answers
    6. Assert invariants (GPU, auto-pipeline, global integration, dashboard chat)
    7. Commit all changes: "Phase N: <what changed>"
       git push origin main (zero unpushed — invariant)
    8. If all stopping conditions met → DONE (no ScheduleWakeup)
       Else → ScheduleWakeup:
         270s  if a KB sweep or build is in-flight (stay in cache window)
         1200s for idle ticks (no active work)
```

## Stopping conditions (ALL must be green to stop)

- Fast suite: 0 failed
- Slow suite: 0 failed
- KB level-1 ≥ 99%, level-2 > 0%, level-3 > 0%, wiki_count > 0, patterns_cached
- 7/7 KB question categories return non-empty answers
- GPU enforcement: create_llm_client(codex) raises for build path
- Auto-pipeline enabled=true
- 7/7 global profiles OK (configure_integrations.py)
- Dashboard chat streams a real astro answer

## What this loop will NOT do

- Skip failing tests
- Add mocks or fakes
- Use CPU for inference (except dashboard chat = codex gpt-5.4-mini → haiku-4.5)
- Amend existing commits
- Auto-index projects (only build if needed for KB completeness)
- Use `Workflow` tool
- Use `CronCreate`
- Hardcode `{model: 'opus'}` — let `/model` and session settings handle it

## Output per iteration

```
=== ENGINE LOOP (iter N) ===
Fast suite:   333 passed / 0 failed ✓
Slow suite:   145 passed / 0 failed ✓
KB L1: 100% ✓  L2: 68% ↑  L3: 12% ↑  wiki: 1022 ✓  patterns: ✓
KB Q1 business: 1247 chars ✓
KB Q2 checkout code: 18 related symbols ✓
KB Q3 gRPC: feature trace found 6 entry points ✓
KB Q4 order+campaign: global synthesis 832 chars ✓
KB Q5 bug root-cause: impact_narrative 614 chars ✓
KB Q6 trace AddToCart: semantic_trace 5 hops ✓
KB Q7 related functions: callers 9 found ✓
GPU enforcement: codex → RuntimeError ✓
Auto-pipeline: enabled=true ✓
Global integration: 7/7 profiles OK ✓
Dashboard chat: "astro uses gRPC..." 2341 chars intent=architecture ✓
Committed: abc1234 "Phase 98: KB self-healing sweep + engine-loop skill"
Pushed to origin/main ✓
L2/L3 still incomplete → ScheduleWakeup 270s (sweep in-flight)
```

Run the loop now.
