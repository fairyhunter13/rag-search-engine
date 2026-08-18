---
type: Component
resource: src/rag_search/daemon/
title: "daemon: when work happens"
description: Nine files, sweeps.py two thirds of them — the watcher that is the only steady-state trigger, the dedicated graph lane that makes queue-empty a lie, and the two places a thread parks forever on purpose.
tags: [daemon, sweeps, watcher, graph-lane, idle, reconcile]
status: active
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# daemon: when work happens

There are no timers driving work. A file changes, the watcher fires, and everything else is a
consequence — the ordering and the drift gate are
[the graph lane wakes only on code drift](../constraints/the-graph-lane-wakes-only-on-code-drift.md).
The cheap half calls into [index](index-package.md), the expensive half into
[graph](graph.md), and the stamps it compares are owned by both.

## Queue-empty is not idle

The graph re-derive runs on a dedicated lane rather than a dispatch worker, because `_HEAVY_LOCK`
serialises the heavy half process-wide anyway — running it on a worker bought no parallelism and
only meant a worker sat *blocked*. Measured on the live daemon: one worker inside `bounded_parse`
holding the lock, a second parked on it for four minutes, and a third project's edit never indexed
because both workers were gone.

So a reader must check `_graph_lane_busy`, not the work queue. `_graph_lane_join()` is the only
correct way to read `graph.db` immediately after `on_change` returns; draining the queue reads a
half-derived graph.

`_HEAVY_LOCK` is never held around index/embed or a GPU query. Moving it there turns a bounded
settle into an unbounded one.

## One inotify instance, and Rust does the coalescing

A single `watchfiles.watch()` generator covers every root — not one watcher per root. Storms
coalesce in Rust before crossing into Python, and `watch_filter` drops ignored paths through the
same resolver the drift gate uses, so ignored-directory churn never reaches `on_change` at all.
Detail: [the watcher is one generator in one thread](../constraints/the-watcher-is-one-generator-in-one-thread.md).

## Two threads that park on purpose

- `server._reconcile_park` is an `Event` that is **never set**. Periodic resync is off by default,
  so the reconcile thread runs once at startup and then waits forever. This is the design, not a
  missing `set()`.
- `runtime_state` must never re-acquire a process idle-exit. A prior `sys.exit(0)` ran in the
  scheduler's background thread, where `SystemExit` is swallowed — it terminated nothing and looked
  like it worked. Idle savings come from `server._idle_unload` only.

## The prompt has a second copy

`global_prompt._PROMPT` mirrors `CANONICAL_BODY` in `scripts/integrations/canonical.py` and the two
move in lockstep. One updated alone means the MCP client and the installed `CLAUDE.md` disagree
about the tool protocol.

## Guards

| Claim | Guard | File |
|---|---|---|
| `on_change` does not block on the heavy lock; the lane waits without a timeout | `test_kl1_on_change_does_not_block_on_the_heavy_lock`, `test_kl4_the_lane_waits_without_a_timeout` | `test_graph_lane.py` |
| the park event is wired; heavy passes serialise; a save storm is one pass | `test_reconcile_park_event_wired`, `test_heavy_lock_serializes_concurrent_passes`, `test_wt3_batch_coalescing_single_call_per_burst` | `test_idle_stability.py` |
