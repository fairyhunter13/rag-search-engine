---
type: Defect
resource: src/coderag/server.py, tests/test_server_tools.py
title: The scheduler suppressed its own death, and it is the one failure no registry row can carry
description: "`contextlib.suppress(Exception)` around the hourly sweep and the per-tick watcher rearm dropped the exception with no log line — and the sweep is what would have written the registry row, so a freshness mechanism that raised every hour left `/healthz` green and every project reading clean."
tags: [observability, scheduler, alerting, resolved]
status: stable
generated: { by: claude/opus-5, at: 2026-08-23T01:40:00Z }
---

# The one error with nowhere to be recorded

Both scheduler jobs ran inside a bare `contextlib.suppress(Exception)`. The intent is right — one
bad job must not stop the timer, which is the same reason the indexer catches per-job — but the
indexer logs the traceback and calls `record_error` before moving on, and this dropped it entirely.

That makes the sweep the worst place in the daemon for a swallowed exception. `record_error` writes
against a project, and the sweep is what walks projects: if `federation.sweep` or
`index.reconcile_all` raises, no project is reached, so no row is flagged. Every enabled row keeps
its last successful state, `last_error` stays null across the fleet, and the freshness mechanism can
be dead for weeks while `/healthz` reports 0 failing. The per-tick `watch.start()` has the same
shape — a watcher thread that fails to start on every tick leaves the fleet with no inotify at all
and nothing said.

This is strictly worse than
[the ephemeral `last_error`](a-failure-that-resolved-itself-left-no-trace.md) that preceded it.
There the evidence was erased within the hour; here it was never written, and the alert built on
those counters could not have seen it either.

# What covers it now

`_guarded` logs the traceback and records the failure under the job's name, so a watcher that
recovers does not clear a sweep that is still dead. `/healthz` returns `scheduler_errors`, and the
health check folds them into the failing set as `scheduler:<job>` — the same two-sample persistence
rule as a project, described in
[a fleet alert decides on two samples](../constraints/a-fleet-alert-decides-on-two-samples-not-on-a-count.md).

Four tests, each confirmed against the old suppression rather than only against the fix: the
recording arm and the `/healthz` arm both fail when the exception is dropped, the per-job clearing
arm fails when one key is used for both, and the fourth holds the property the suppression existed
for in the first place — a raising job must not stop the timer.
