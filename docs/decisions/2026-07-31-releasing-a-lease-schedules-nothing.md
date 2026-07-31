# Releasing a pause lease schedules nothing

**2026-07-31** · P3 · mechanism: `daemon/server.py` `_reconcile_loop`, `daemon/sweeps.py:1044`

A stamp move (`e7` + `fg3`) was committed under a sweeps pause lease held across the whole edit
session, exactly as intended — the daemon must not see an intermediate state and start a re-derive
per commit. The lease was then released so the fleet could converge.

It did not converge. Forty-five minutes later the fleet still read **147 of 150 stores stale**, and
every diagnostic said the daemon was healthy:

```
cpu_percent_core: 0.0193      idle_seconds: 459.8
sweeps_pause_lease_s: 0.0     active_clients: 0     ok: true
```

No error, no warning, no failed unit. A daemon with nothing to do looks exactly like a daemon that
has finished.

## What actually happened

```
18:08:57  Started rag-search-mcp-daemon.service          (a CI run restarted it)
18:09:37  reconcile: abandoned before start (sweeps paused)
18:12     POST /api/sweeps/resume  ->  previously_paused: true
```

The reconcile pass is **startup-once**. `_reconcile_loop` sleeps out an initial grace delay, runs
`reconcile_projects()` exactly once, and then parks on an Event that is never set. Periodic resync
is opt-in and off by default (`RSE_RECONCILE_RESYNC_S = 0`) because steady state is watcher-driven
via `on_change` — a design that is right, and that has no bearing on a stamp move, because moving
`EXTRACTOR_REV` changes no file on disk and therefore produces no watcher event at all.

So the single pass that was ever going to run fired *inside* the lease window, took the
`is_paused()` early return, and parked. The release three minutes later permitted work that nothing
remained to trigger.

**A lease release is a permission, not a schedule.** Nothing re-reads it. The plan step "release the
lease after the last commit" quietly assumes the daemon will come past again; on this daemon, after
its one-shot has run, it will not.

## Why it is silent rather than loud

Three things have to line up, and each is individually correct:

- The abandon logs once, at INFO, into a journal whose steady state is a `/healthz` line every 20 s.
  It is one line in several hundred, and it scrolls past before anyone looks.
- `/healthz` has no term for "the re-derive that was owed never ran". `sweeps_pause_lease_s: 0.0`
  reports the lease is gone, which is true and reads like success.
- `stale_stores` is per-project, under `overview(what="metrics")`. Asking the fleet-level question
  means asking it 150 times, so in practice nobody asks it and the number that would have shown
  this instantly is the one not on screen.

## The rule

**After a stamp move: release the lease, then restart the daemon.** `POST /api/reload` (no lease is
held by then, so no 409) — the startup-once reconcile fires again, this time unpaused, and the fleet
converges. Then watch the stale count *fall*; the absence of errors is not evidence, because this
failure produces none.

Do not infer from a healthy `/healthz` that a re-derive ran. The only positive evidence is stores
changing stamp, or `_rederive_graph … re-extracted and re-detected` in the journal.

## Not the same hazard as the one already written down

`CLAUDE.md` warns that `systemctl --user restart` clears `_PAUSED` silently and that the one-way
renewer never re-arms — a restart destroying a lease that should have held. This is the inverse: a
*legitimate* restart whose one-shot reconcile lands inside a lease window that is doing its job, and
is lost. Same two mechanisms, opposite direction, and the existing warning does not cover it — it
tells you to re-pause after a restart, which is exactly what keeps this one hidden.

## Rejected: turn on periodic resync

Setting `RSE_RECONCILE_RESYNC_S > 0` would have caught it, and it is the wrong trade: a timer
re-walking every project forever, to fix something that happens once per stamp move and is already
fixed by one restart at a moment the operator chooses. Periodic resync is off deliberately —
re-deriving on a clock is what the watcher-driven design exists to avoid.
