# Live fast-lane cost baseline (2026-07-31)

Step 7a of the suite-speed plan: measure before converting anything. The plan predicted that
fixture scope dominated the fast lane and that consolidating corpora was the big win. **It does not,
and it is not.** This file records the measurement and what it kills, so the next person does not
re-derive a lever that was already refuted.

## The lane

`slow` no longer exists. It was split on 07-30 into `costly` (spends real Claude quota) and
`exclusive` (needs a quiescent daemon), so the old `-m "live and not slow"` is rejected outright by
`--strict-markers`. The fast lane is:

```
pytest src/tests/live/ -m "live and not costly and not exclusive" \
  --ignore=src/tests/live/test_browser.py --durations=0 --strict-markers --strict-config -ra -q
```

## Preconditions (without these you measure the host, not the suite)

1. **Suite lock clear** — no other pytest with `cwd` == this repo. Identity by `argv[0]`, not
   `pgrep -f`, which matches the waiter itself.
2. **>= 10,300 MiB free VRAM** — the suite loads a real embedder + reranker in-process.
3. **GPU utilisation <= 20%** — a baseline taken against a daemon mid-rebuild measures the rebuild.

Not optional. The **same lane, same markers** took **23+ minutes** against a contended host and
**157 s** against a quiet one — a ~9x spread with no code difference. Anything measured without
gate 3 is noise. Nothing in the runner may call the daemon: an MCP request resets the 300 s idle
timer that is the only path to ORT releasing its arena.

## Result

```
5 failed, 599 passed, 22 deselected in 157.39s (0:02:37)
```

Accounting: 969 duration rows summing **156.4 s of 157.4 s** wall — essentially complete.

| Phase | Time | Share |
|---|---|---|
| `call` | 136.6 s | **87.3%** |
| `teardown` | 11.7 s | 7.5% |
| `setup` | 8.1 s | **5.2%** |

| Cost by file | Time | Share |
|---|---|---|
| `test_graph_lane.py` | 37.8 s | 24.2% |
| `test_federation_exclude.py` | 18.2 s | 11.6% |
| `test_bounded_parse.py` | 14.8 s | 9.4% |
| `test_p21_integration_parity.py` | 9.8 s | 6.3% |
| `test_idle_stability.py` | 9.1 s | 5.8% |
| `test_cast_chunking.py` | 8.1 s | 5.2% |
| top 12 files | 122.0 s | **78%** |

**31 tests take >= 1.0 s and account for 109.4 s (70%).** 843 more are below 5 ms each.

## The prediction, and why it was wrong

The plan asserted: *"69 tests each build their own real corpus (1.4–10.3 s of measured GPU work
apiece) while 17 share the session-scoped one — that is the fast lane's bill."*

`safe_tmp_path` (`conftest.py:409`) is `make_run_dir()` plus a teardown purge. **It yields an empty
directory. It indexes nothing.** The per-index costs the plan cites are real, but they were taken
from the *daemon's production journal* and applied to a population that does not do that work. The
number that settles it is measured, not argued: **total setup across the whole lane is 8.1 s.**
Perfect fixture promotion — every setup to zero — would save at most 5.2%.

This is `feedback_measure_with_production_defaults` committed inside the document that cites it:
production's per-unit cost, multiplied by the wrong population.

## What actually costs, and why it is irreducible without mocks

Reading the 31 tests >= 1 s: `kl1` "on_change does not block on the heavy lock" (21.5 s), `kl3`
"submits coalesce" (10.1 s), `kl2` "the deferred pass still happens" (6.2 s), `wt1`/`wt3` watcher
debounce and batch coalescing, `bp1`/`bp3`/`bp4` and the parse-pool timeouts, `gb1`/`gb2` GPU release
and EP rebind, `loop1`/`loop2` healthz responsiveness under load.

Every one of these has **elapsed time or concurrency as its subject**. The wait *is* the assertion.
Sharing a fixture cannot shorten it; only shortening the real debounce/timeout/unload window can,
and that window is the production constant under test. Faking it is banned by
`test_no_mocks_or_fakes.py` and would delete the test's meaning anyway.

## Consequences for the plan

- **S1 (corpus promotion) — dropped, not re-ordered.** Ceiling of 5.2%, and it trades that for a
  shared mutable corpus and cross-talk risk. Prefer no change.
- **S3 (one stream per distinct prompt) — does not touch this lane.** Those tests are `costly` and
  are among the 22 deselected here. Any benefit lands in the costly lane; measure that separately
  before claiming it.
- **S4 (xdist) — no.** Its precondition was that the bulk group is I/O-bound and parallelisable. The
  843 sub-5 ms tests have nothing to gain, and the 31 that dominate are exactly the daemon-mutating,
  timing-sensitive ones that must stay serial.
- **S6 — the time-based residue was estimated at ~5 tests. It is the top ~20, and they already run
  in the fast lane on every push.** There is nothing to un-hide here.
- **S5 (`scripts/affected_tests.py`) — shipped in `c7caab9`**, and it stays an inner-loop convenience,
  never a gate. See below for why that matters more than it looks.

**The fast lane cost 157 seconds on a quiet host at `d9860a0`. It did not need optimising.** What
made it feel slow was host contention and one test that hung for 51 minutes
(`test_reconcile_midpass.py`, fixed in `071da96` — it called `reconcile_projects()` in-process, so it
walked the entire 216-row fleet).

## Re-measured 40 minutes later — and the number moved 3.2x

Same lane, same markers, same `HEAD`:

| | 03:0x, at `d9860a0` | 03:46, at `d9860a0` |
|---|---|---|
| selected | 604 | **848** (+244) |
| wall | 157.4 s | **497.3 s** (+216%) |

`HEAD` did not move. All 244 tests came from another session's **uncommitted** working-tree edits —
12 modified live test files plus one untracked new file. The new tests cost **~1.39 s each** against
the incumbent average of **0.26 s**: roughly 5x more expensive per test than what was already here.

**This finding outranks everything above.** The plan's premise was that the fast lane needed
structural work, and its largest lever (S1, corpus promotion) had a measured ceiling of 5.2% — about
8 seconds. In the 40 minutes spent proving that, the suite grew by 340 seconds. **Growth is the cost
driver; per-test inefficiency is not.** Future effort aimed at making this suite fast belongs on what
new tests are allowed to cost, not on re-plumbing the fixtures of the ones already here.

Corollary for this document: a baseline is a perishable measurement of a moving tree. Quote it with
its commit *and* its collected count, and re-measure rather than cite. The count is what catches
drift — a wall clock alone cannot tell "the host was busy" from "the suite grew".

## The single most expensive test: WK2, at 59 s

`test_watcher_registry_sync.py::test_wk2_http_the_daemon_reports_no_root_armed_for_a_disabled_project`
takes **59.08 s — 12% of the whole lane**, and more than a third of what the entire lane used to cost.

Its docstring claims the opposite: *"It costs nothing in the ordinary case — the first sample is clean
and the loop returns at once — and the budget is only ever spent on a real divergence."* Measurement
refutes that. The test polls `GET /api/watcher` until `extra == []`, and the armed set reaches the
registry only through the `watcher_sync` scheduler job, registered at `interval_s=60.0`
(`daemon/server.py:208`). 59.08 s is one full scheduler period — exactly what the mechanism predicts.

The docstring's error is in *population*, not arithmetic: it assumed divergence is exceptional, but
**the live suite itself registers and disables temp projects throughout the run**, so a dirty armed
set is the normal state by the time WK2 observes it. The test's cost is imposed by the rest of the
suite's churn, not by its own subject.

There is no cheap fix inside the test. `/api/watcher` is GET-only (`server/routes_admin.py:36`) and
nothing can force a sync. The honest options are to add a real ops action — `POST /api/watcher/sync`
calling the same `sync_watcher` the scheduler calls, which an operator hitting the original incident
would also want, since today the only remedy is a daemon restart — or to accept the 59 s. Shortening
the 60 s interval is not on the table: that interval is the production behaviour under test.

## The other half of the result: the failures

Four of the five failures were regressions introduced *earlier in the same session*, by commits whose
own verification had been a hand-picked subset of 6-8 files. The subset was green. The full lane was
not. Specifically: two static guards (`test_no_raw_sweeps_toggle.py`), the no-skip policy guard
(`test_no_code_semantic_regex.py`), and a contract assertion in `test_p5_server.py`.

At 157 s there is no case for ever verifying against a subset. **Run the lane.**
