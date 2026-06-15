# ux-journey-loop skill

Autonomous loop: run predefined Playwright user journeys + MCP client-surface e2e → find RED → investigate → fix → commit + push → repeat until 2 consecutive fully-green passes.

## What this loop tests

The **spec** is the predefined test cases in `src/tests/live/test_browser.py` (all `test_journey_*` functions) and the G3/G4/G5 `/mcp` round-trips in `test_mcp_protocol_http.py`. New journeys are added to those files, never invented ad hoc by this loop.

### Journey inventory
- `test_journey_user_empty_chat_is_ignored` — empty Enter must not add a chat bubble
- `test_journey_reader_toggles_wiki_lint` — wiki-lint header toggles `.open` class
- `test_journey_analyst_filters_graph_to_files` — graph filter hides non-file nodes
- `test_journey_structure_tile_shows_files_with_symbols` — pulse KPI renders `files_with_symbols`
- `test_journey_operator_reindex_sees_completion` — Re-index produces `.ok` op-log line
- `test_journey_operator_reindex_sees_job_chip` — SSE job event produces `.admin-chip`
- `test_journey_user_asks_and_gets_progressive_answer` — chat stream replaces Thinking…
- `/mcp` round-trips: G4 unknown-what error, G5 no-path graph resolution, G3 key renames

## Loop body (runs without pausing for confirmation)

```
green_streak = 0
while green_streak < 2:
    1. ARM: GET /healthz (fail-fast if daemon down); POST /api/reload (load latest code); wait for /healthz again
    2. QUALITY GATE: ruff check + compileall (fast fail before browser)
    3. RUN JOURNEYS:
       pytest src/tests/live/test_mcp_protocol_http.py -m live -q
       pytest src/tests/live/test_browser.py -k "test_journey" --browser chromium -q
             --screenshot=only-on-failure --tracing=retain-on-failure
    4. If 0 failures:
       green_streak += 1
       if green_streak < 2: ScheduleWakeup(270, "ux-journey re-arm: pass N/2"); stop
    5. For each RED: INVESTIGATE (screenshot/trace in test-results/; search()/Read handler code;
       classify code-bug vs UX-regression vs test-gap)
    6. FIX minimally (lean-change; lean-gate rejects >40 net lines per Edit → split)
    7. Re-run just the failed journey to green
    8. Loop to step 3
    9. If same journey still RED after fix → stop and escalate to user
    10. On 2 consecutive passes: git add + commit + git push origin main; stop
```

## Thermal-aware execution

The browser suite is light (no LLM except DB7b's one chat stream). Before running DB7b, check GPU temp: `nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader`. If > 70°C, defer — the daemon reload is enough to keep other journeys warm.

## What this loop will NOT do

- Skip any journey or weaken an assertion to make it pass
- Add mocks or fakes
- Use CPU for inference (GPU-only; RTX 5080 CUDA)
- Let codex/claude-haiku outside dashboard chat path
- Raise the GPU thermal guard (85°C inference / 82°C indexing are hard limits)
- Amend existing commits — always creates new ones
- Invent new journey test cases — edit `test_browser.py` or `test_mcp_protocol_http.py` and restart the loop instead
- Use Workflow, CronCreate, or cloud routines — `/loop` + ScheduleWakeup only

## Output per iteration

```
Pass 1: 2 RED
  test_journey_analyst_filters_graph_to_files: FIXED (root cause: __graph not initialized before filter)
  test_journey_operator_reindex_sees_job_chip: FIXED (root cause: SSE queue removed before event published)
  Journeys: 7 passed, 0 failed ✓
  /mcp round-trips: 4 passed, 0 failed ✓
  Committed: abc1234 "fix: graph filter init + SSE queue lifetime"
  Pushed to origin/main ✓
  green_streak=1 — re-arming in 270s
Pass 2: 0 RED
  green_streak=2 — loop complete ✓
```

## Complementary loops

- `/fix-loop` — fast unit/integration suite (no browser)
- `/engine-loop` — KB completeness + engine surface probes
- `/issue-sweep` — latent issues not covered by any test
- `/ux-journey-loop` — this skill: dashboard UX journeys + MCP client-surface e2e

Run the loop now.
