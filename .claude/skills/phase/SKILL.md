# phase skill

Execute a complete development phase loop: detect → fix → verify → commit → push.

## Input

Optionally pass a description of what to build or fix:
```
/phase fix flaky GPU residency test
/phase add new feature X
/phase  (no args = audit current state and fix anything broken)
```

## Loop

### Step 1: Orient
- Call `overview(what='projects')` — confirm registry clean
- Run fast test suite — find any failures
- Check GPU state and daemon metrics
- If everything is clean: report "System clean — nothing to do" and stop

### Step 2: Identify work
- If test failures: classify each as code bug / infra issue / test gap
- If feature work requested: read relevant code with `search()` first
- Never start coding without reading current state first

### Step 3: Fix / implement
- Fix code bugs — minimal change, no extra refactoring
- For infra failures: restart services, then rerun tests to confirm
- After each edit: run the specific failing test(s) to verify fix
- No mocks, no skips, no CPU fallback

### Step 4: Full verify
- Run complete fast suite: `pytest src/tests/live/ -m "live and not slow" -q --ignore=src/tests/live/test_browser.py`
- Must be 0 failures before proceeding

### Step 5: Commit & push
- `git add` only changed source files (not .env, not generated files)
- Commit message: "Phase N: <concise summary of what changed>"
- `git push origin main` immediately — zero unpushed commits policy
- Confirm push succeeded

### Step 6: Report
- What changed (file-level diff summary)
- Test result: N passed, 0 failed
- Commit hash and push confirmation

## Constraints

- GPU only — no CPU fallback ever
- Real tests only — no mocks
- Push after every commit — zero unpushed policy
- Fix root causes, not symptoms
- Never add features unless the user asked for them

Execute the phase loop now.
