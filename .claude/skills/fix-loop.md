# fix-loop skill

Autonomous loop: run tests → find failures → fix them → commit → repeat until clean.

## Behavior

This skill runs without pausing for confirmation. It loops until:
- All tests pass, OR
- It finds a failure it cannot fix (then it stops and reports)

## Loop body

```
while failures exist:
    1. Run fast tests — collect all failures
    2. For each failure (in dependency order):
       a. Read relevant source with search() first
       b. Identify root cause (code bug vs infra vs test gap)
       c. Apply minimal fix
       d. Run just that test to confirm green
    3. Run full fast suite — if clean:
       a. Commit: "fix: <summary of what was fixed>"
       b. git push origin main
       c. Done
    4. If same test still fails after fix: escalate to user
```

## What it will NOT do

- Skip failing tests
- Add mocks
- Use CPU for inference
- Amend existing commits
- Touch tests it didn't break

## Output per iteration

```
Iteration 1: 3 failures found
  - test_X: fixed (root cause: Y)
  - test_Y: fixed (root cause: Z)
  - test_Z: fixed (root cause: W)
  Fast suite: 330 passed 0 failed ✓
  Committed: abc1234 "fix: resolve X Y Z"
  Pushed to origin/main ✓
```

Run the loop now.
