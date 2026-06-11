# engine-loop skill

Autonomous full-coverage loop: probe every search-engine surface against astro-project → fix any RED → commit + push → re-arm → repeat until 100% green.

## June 2026 loop features in use

- **ScheduleWakeup** dynamic `/loop`: re-arms with cache-window-aware delays (270s while watching in-flight KB sweep; 1200s for idle ticks)
- **SessionStart + Stop + PostCompact hooks** (from `.claude/settings.json`): inject branch/daemon/GPU/unpushed context on every wakeup — the loop is always cold-start-aware
- **fallbackModel + effortLevel**: session settings, not hardcoded here; `/model opus` activates Opus when investigating failures
- No `Workflow`, no `CronCreate`, no `workflows/` scripts

## Current stack (June 2026)

- **ollama 0.30.7** (upgraded from 0.24.0; speed-neutral, newer Blackwell backend). Rollback binary: `/usr/local/bin/ollama.0.24.0.bak`.
- **Model-tier MAP**: global map-reduce runs MAP on `qwen3-enrich:1.7b`, REDUCE on `qwen3-query:8b` → global synthesis ~65s (was 276s). See `docs/PERFORMANCE.md`.
- **Thermal config** (systemd drop-in `…/opencode-search-mcp-daemon.service.d/thermal-max.conf`): `OPENCODE_GPU_TEMP_MAX=85` (inference) / `OPENCODE_EMBED_MAX_TEMP=82` (indexing). GPU hardware limits: Tjmax 89°C, slowdown 100°C, shutdown 103°C.

## Thermal-aware execution (cool-mode safe — MANDATORY for heavy runs)

This is a **firmware-locked MSI laptop** (RTX 5080 Laptop, sm_120). Power limit and fan
control are NOT user-settable (`nvidia-smi -pl` → "not supported"; `msi-ec` rejects the
firmware). The slow suite is the hottest load and **heat-soaks the whole chassis** (after
long runs: GPU 65°C+ at idle, CPU 72–74°C, **SSD ~79°C** — SSDs throttle ~70-80°C). So:

- **Before a slow-suite run, check GPU temp** (`nvidia-smi --query-gpu=temperature.gpu`).
  If GPU > ~70°C (heat-soaked), **let it idle-cool first** — a hot start makes the run
  slower (more thermal pauses) and stresses the SSD/chassis.
- **Never run heavy suites back-to-back** without an idle cooldown between them.
- The slow suite's `test_global_intent_slo` already gates on `_await_gpu_cool()` so its
  SLO is measured under nominal (non-throttled) conditions — keep that pattern for any
  new latency-SLO test.
- The 85/82 software guards pause inference when hot; the hardware 100/103°C limits are
  the real backstop. **Do NOT raise the guards toward 90°C** to "go faster" — the laptop
  is thermal-bound, not compute-bound; cooling (a cooling pad) is the only real lever.
- If the user is in "cool mode", **defer the slow suite** until the laptop has cooled and
  prefer the fast suite for iteration.

## Inference-efficient testing (no mocks, no coverage loss)

Comprehensive real-LLM coverage is inherently inference-heavy (and hot). When adding or
fixing slow tests, **reduce redundant inference without mocking or dropping coverage** —
the "compute the real artifact once, assert many properties against it" pattern:

- **Build the enriched KB once per session** (session-scoped fixture) instead of each
  test re-triggering enrichment.
- **Judge shared answers once** — don't re-synthesize + re-judge the same answer in
  multiple tests; assert against one real, judged golden answer.
- **Canonicalize near-duplicate queries** (e.g. "overall architecture" asked 4 ways) →
  one real synthesis shared, plus a couple of cheap phrasing-robustness checks.
- **Classify-only for pure routing tests** — a test that only asserts "query → intent"
  doesn't need the full 8B synthesis.
- Real LLM output, **never mocks**; **never skip**; **never continue-on-error**; 100%
  edge-case coverage preserved. The goal is fewer *redundant* calls, not fewer scenarios.

## What this loop verifies

### 1. Test suites
- Fast live suite: `.venv/bin/pytest src/tests/live/ -m "live and not slow" -q`
- Slow non-browser suite: `.venv/bin/pytest src/tests/live/ --ignore=src/tests/live/test_browser.py -q`

### 2. KB completeness — auto-queue done (the core requirement)
Poll `GET /api/kb_health?project=.../astro-project`. Check `enrichment_by_level`:
- **Every level** must be ≥ 99% enriched (L1 + L2 + L3 all ≥ 99%)
- `wiki_page_count > 0`
- `patterns_cached = true`

The daemon's `_run_kb_sweep` (every 6 h; first run within the startup window) now
fully converges enrichment: L1-drain-before-L2+, loop-until-dry, dedup guard.
**This loop must NOT POST /api/enrich_project or /api/enrich_hierarchy** — those are
internal daemon surfaces. The loop's job is to observe and report. If any level is
below 99%, report it and wait; reload the daemon to arm the startup sweep immediately.
The CLI `opencode-search kb-status --project <path>` prints the DONE/PENDING verdict.


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

### 5. Search SLO and GPU isolation health
```bash
LOG=~/.local/state/opencode-search/daemon.log
echo "embedding reloads: $(grep -c 'Loading embedding model' $LOG)"
echo "CUBLAS errors:     $(grep -ic 'cublas' $LOG)"
```
`curl -s http://localhost:8765/api/metrics` — check `cublas.hard_cooldowns_entered == 0` (or stable/not growing).
Live SLO probe: `curl -s "http://localhost:8765/api/search?q=authentication+handler&project=<astro>&top_k=5"` → assert `elapsed_ms < 5000`.

If CUBLAS errors are growing or search takes > 5s:
- The query-embedder isolation fix may have regressed.
- Reload the daemon (`POST /api/reload`) to reinitialise the pinned query sessions.
- This loop must NOT restart the daemon service — only the HTTP reload endpoint.

### 6. Storage health
Poll `GET /api/storage_health?project=<astro-project>`. Assert:
- `stale_index_dirs ≤ active_index_count + 2` (no unbounded accumulation of dead `_indices/` UUIDs)
- `wal_bytes < 67108864` (WAL under 64 MB — bounded by `journal_size_limit` pragma)
- `recoverable_mb < 50` (less than 50 MB of recoverable waste per project)

Storage is fully automatic — the maintenance sweep reclaims stale dirs and bounds the WAL (every 6 h;
first run within 60 s of daemon startup). There is no manual vacuum trigger. If a threshold is
exceeded: report it and wait for the next sweep (each loop iteration that commits + reloads the
daemon re-arms the 60 s startup sweep). The thresholds are convergence targets, not instant gates.

## Loop body

```
while stopping conditions not met:
    1. Run fast suite → collect failures
    2. For each failure:
       a. search() source code first (mandatory — MCP before bash/read)
       b. Classify: code-bug vs infra (cold Ollama model → reload qwen3-query:8b)
       c. Minimal fix → rerun that test → confirm green
       d. Never skip, never mock, never CPU fallback
    2b. THERMAL GATE before the slow suite (heaviest/hottest load):
        nvidia-smi --query-gpu=temperature.gpu,utilization.gpu --format=csv,noheader
        - If GPU > ~70°C (chassis heat-soaked) OR the user is in "cool mode":
          idle-cool first (or defer the slow suite this iteration and report).
        - A hot start = more thermal pauses (slower) + SSD/chassis stress.
    3. Run slow non-browser suite → fix any RED (same rules)
    4. Check KB convergence: GET /api/kb_health → check enrichment_by_level
       every level must be ≥ 99%. If not: report it; reload the daemon (arms the
       startup sweep within a few minutes); wait and re-poll.
       Use `opencode-search kb-status --project <astro>` for a compact DONE/PENDING view.
       DO NOT POST /api/enrich_project or /api/enrich_hierarchy — the daemon owns convergence.
    5. Check search SLO + GPU isolation health:
       - Probe /api/search → assert elapsed_ms < 5000
       - Check daemon log CUBLAS error count is not growing
       - If growing: reload daemon to reinitialise pinned query sessions; report
    6. Probe all 7 KB question categories → assert non-empty answers
    7. Assert invariants (GPU, auto-pipeline, global integration, dashboard chat)
    7b. Check storage health (GET /api/storage_health):
        stale_index_dirs ≤ active+2, wal_bytes < 64 MB, recoverable_mb < 50
        → if red: report it; sweep fires within 60 s of next daemon reload (no manual trigger)
    8. Commit all changes: "Phase N: <what changed>"
       git push origin main (zero unpushed — invariant)
    8. If all stopping conditions met → DONE (no ScheduleWakeup)
       Else → ScheduleWakeup:
         270s  if a KB sweep or build is in-flight (stay in cache window)
         1200s for idle ticks (no active work)
```

## Stopping conditions (ALL must be green to stop)

- Fast suite: 0 failed
- Slow suite: 0 failed
- KB **every level ≥ 99%** (L1, L2, L3 all ≥ 99%), wiki_count > 0, patterns_cached
- 7/7 KB question categories return non-empty answers
- GPU enforcement: create_llm_client(codex) raises for build path
- Auto-pipeline enabled=true
- 7/7 global profiles OK (configure_integrations.py)
- Dashboard chat streams a real astro answer
- Storage: stale_index_dirs ≤ active+2, WAL < 64 MB, recoverable_mb < 50

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
KB L1: 100% ✓  L2: 99% ✓  L3: 99% ✓  wiki: 1022 ✓  patterns: ✓  verdict: DONE ✓
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
Storage: 2 active / 2 on-disk idx, WAL 8MB, 0MB recoverable ✓
Committed: abc1234 "Phase 98: KB self-healing sweep + engine-loop skill"
Pushed to origin/main ✓
L2/L3 still incomplete → ScheduleWakeup 270s (sweep in-flight)
```

Run the loop now.
