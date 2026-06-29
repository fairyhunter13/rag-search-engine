# issue-sweep skill

Autonomous proactive discovery-and-fix loop: scan for latent issues (no test required) → root-cause → fix → verify signal gone → commit + push → repeat until 2 consecutive clean sweeps.

Complements `/engine-loop` (known-surface regression guard) and `/fix-loop` (failing-test guard).
This skill hunts issues **no test covers yet** — log storms, CUBLAS thrash, WAL growth, stale index dirs, import cycles, silent fallbacks, CPU-fallback violations.

## When to run

- After a major refactor or GPU-path change (fixes may introduce new thrash).
- On suspicion that "everything passes but something feels wrong."
- As a scheduled quality sweep to stay ahead of slow-burn regressions.
- Never inside a Workflow or CronCreate — `/loop issue-sweep` is the right invocation.

## Loop body

```
dry_streak = 0
while dry_streak < 2:
    findings = scan()        # multi-modal, all angles
    findings = analyze(findings)  # root-cause, dedup, rank
    if not findings:
        dry_streak += 1
        wait(short)
        continue
    dry_streak = 0
    for issue in findings (highest severity first):
        fix(issue)           # minimal, no mocks, no skips, GPU-only
        verify(issue)        # confirm signal gone + no regression
        commit_and_push()    # zero-unpushed invariant
```

## Phase 1 — Scan (multi-modal, run all angles in parallel)

### 1a. Runtime signals (daemon log + /api/metrics)
```bash
LOG=~/.local/state/opencode-search/daemon.log
echo "embedding loads:  $(grep -c 'Loading embedding model' $LOG)"
echo "reranker loads:   $(grep -c 'Loading reranker model' $LOG)"
echo "CUBLAS errors:    $(grep -ic 'cublas' $LOG)"
echo "tracebacks:       $(grep -c 'Traceback' $LOG)"
echo "GPU OOM:          $(grep -ic 'out of memory\|OOM\|CUDA error' $LOG)"
```
`curl -s http://localhost:8765/api/metrics` — check:
- `cublas.hard_cooldowns_entered > 0` → ONNX thrash (high severity)
- `chat_stream.stream_error_count > 0` → streaming failures
- `error_by_intent` non-empty → intent-routing errors
- `gpu_temp_c > 85` → thermal throttle active

Flag if embedding-model reload count > 10 (indicates session eviction storm).

### 1b. Correctness (tests + static)
```bash
.venv/bin/pytest src/tests/live/ -m "live and not slow" -q   # fast live suite
.venv/bin/ruff check src/opencode_search src/tests            # lint
.venv/bin/python -m compileall -q src/opencode_search         # syntax
```
Use `/run-all-tests` for the full live suite when investigating a known failure.

### 1c. Data / KB health
```bash
opencode-search kb-status --json              # per-project DONE/PENDING
```
`curl -s "http://localhost:8765/api/storage_health?project=..."` — check:
- `stale_index_dirs > active_index_count + 2`
- `wal_bytes > 64MB`
- `recoverable_mb > 50`

### 1d. Code intelligence (dogfood the engine) — call MCP tools
```
overview(project_path, what='import_cycles')          # circular deps
overview(project_path, what='surprising_connections')  # cross-layer edges
overview(project_path, what='graph_diff')              # recently changed symbols
graph(hot_symbol, project_path, relation='impact_narrative')  # blast radius
ask('what is the most fragile or riskiest code right now?', project_path, scope='global')
```

### 1e. Code smell grep
```bash
grep -rn "except:\s*$\|except Exception:\s*pass\|# type: ignore\|TODO\|FIXME\|HACK\|cpu_fallback\|fallback.*cpu\|device.*cpu" src/opencode_search/ | head -30
# CPU-fallback violations (hard rule: GPU-only)
grep -rn "providers.*cpu\|CPUExecutionProvider\|device.*'cpu'" src/opencode_search/ | grep -v test | head
```

### 1f. MCP self-test (dogfood each of the 5 tools)
```
search('authentication handler', project_path)  → non-empty results
ask('how does search work?', project_path)       → non-empty answer
graph('embed_query', project_path, relation='callers')  → non-empty
overview(project_path, what='structure')         → file_count > 0
index(project_path, enabled=True)               → SKIP (write op — do not call in sweep)
```
Flag any tool returning error/empty.

### 1g. Search SLO probe (GPU isolation validation)
```bash
curl -s "http://localhost:8765/api/search?q=authentication+handler&project=<YOUR_FEDERATION_ROOT>&top_k=5"
```
Assert `elapsed_ms < 5000` (5s SLO). If > 5s while daemon is active: likely CUBLAS thrash
resurfacing — re-investigate GPU isolation.

## Phase 2 — Analyze / Prioritize

For each candidate:
1. Root-cause: is it a code bug, an infra issue, or a test gap?
2. Severity tier (highest first):
   - **P0**: crash / data loss / GPU OOM / silent CPU fallback
   - **P1**: correctness failure / test red / CUBLAS storm / search freeze
   - **P2**: performance SLO miss / KB unconverged / WAL overflow
   - **P3**: hygiene / lint / code smell / TODO in hot path
3. Dedup: same root cause reported by multiple scanners = one item.
4. **Log dismissed false positives** with reason — no silent drop.

## Phase 3 — Fix

For each issue, highest severity first:
- Read the relevant code with `search()` FIRST (mandatory per CLAUDE.md).
- Apply a **minimal** root-cause fix.
- Hard rules (no exceptions):
  - No mocks, no test skips
  - GPU-only — no CPU fallback
  - No `asyncio.Workflow`, `CronCreate`, or cloud routines
  - No `--no-verify` on commits

## Phase 4 — Verify

After each fix:
1. Confirm the **specific signal is gone** (e.g., CUBLAS reload count stops climbing; SLO met; test green).
2. Re-run fast live suite: 0 failed / 0 skipped.
3. `ruff check` + `compileall` clean.
4. If signal persists after fix: escalate to user with root-cause analysis.

## Phase 5 — Commit + Push

Per fix (not per sweep iteration):
```bash
git add <changed files>
git commit -m "fix: <concise root cause and impact>"
git push origin main   # zero-unpushed invariant
```

## Phase 6 — Loop control

- Re-scan after each commit.
- Stop when 2 consecutive full scans find nothing new.
- Self-pace via `ScheduleWakeup` (270s when actively watching a signal; 1200s when idle).
- Never spin: if the same issue reappears after a fix, stop and report — don't loop forever.

## What this skill will NOT do

- Skip failing tests or suppress errors
- Add mocks or CPU fallbacks
- Add `# type: ignore` to silence real bugs
- Amend published commits
- Call MCP write tools (index/build) without the user's explicit request
- Run Workflow, CronCreate, or any cloud routine
- Treat a persistent signal as "acceptable noise" — every repeated error is a finding

## Output per iteration

```
Iteration 1: 3 issues found
  P0 — CUBLAS thrash (3200 errors): fixed (query embedder isolated from passage path)
  P2 — KB L1 stuck at 64.3%: daemon sweep now converging (report-and-wait)
  P3 — bare except: in _wiki.py:142: fixed
  Signals verified gone; fast suite 378 passed 0 failed ✓
  Committed: abc1234 "fix: isolate query embedder from passage path"
  Pushed to origin/main ✓

Iteration 2: 0 new issues (dry_streak=1)
Iteration 3: 0 new issues (dry_streak=2) → DONE
```

Run the loop now.
