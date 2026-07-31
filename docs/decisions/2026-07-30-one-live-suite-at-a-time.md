# One live suite at a time, keyed on process not lock name

**2026-07-30** · guard: `_contending_live_runs`, `src/tests/live/conftest.py`

Multiple agent profiles work in this checkout (`~/.claude`, `~/.claude-1`, `~/.claude-2`), and
two concurrent live suites share one 1-core daemon cgroup, one GPU, one registry and one global
sweep pause, so they contaminate each other's measurements rather than merely running slowly.
`pytest_configure` aborts with a `UsageError` naming the other run's pid and profile.

**Don't wrap runs in `flock`** — that was the previous convention and it failed silently: on
2026-07-30 two sessions each invented their own lock name (`/tmp/rse-live.lock` vs
`/tmp/rse-live-tests.lock`) and so serialised against nobody.

A collision never announces itself as one. It surfaced as:

- CB3 measuring 0.44 core on an "idle" daemon
- a 5 s `/api/metrics` timeout
- 106 pause calls against 4 resumes
- two leaked sample-workspace store sets
- 11 session-setup errors that vanished on re-run

Three of those were chased as regressions first.

**The transferable rule:** the gate keys on the contending *process*, not on a shared lock name,
because both parties can honour a convention to the letter and still collide. A convention that
requires independent parties to guess the same string is not a lock.

Related: [reload under a sweeps lease](2026-07-29-reload-under-a-sweeps-lease.md) is the sibling
collision class this gate does *not* cover — there the colliding party was a bare `curl`, not a
second suite.
