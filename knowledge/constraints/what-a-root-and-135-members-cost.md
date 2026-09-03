---
type: Constraint
resource: src/coderag/server.py, src/coderag/systemd.py
title: What a root and 135 federated members cost, and why MemoryHigh fires 404 times without hurting
description: "Measured 2026-08-20 against the live daemon: 12 searches over a 135-member federation, p50 1.46 s, 91% of one core while working, 2.31 GiB anon. The cgroup sits at its 4 GiB MemoryHigh and has logged 404 high events. The total memory stall across the process's life is 27 ms, because the excess is page cache."
tags: [performance, memory, federation, systemd]
status: stable
generated: { by: claude/opus-5, at: 2026-08-20T21:10:00Z }
---

# The numbers

The one federated root on this machine holds 10142 files, 23223 chunks, 135
members. Twelve searches, five distinct queries, warm daemon, quiet card:

| | |
|---|---|
| latency | p50 1.46 s, p95 2.16 s |
| CPU | 20.1 s over 22.0 s wall — 91% of one core, `nr_throttled` 0 |
| memory | 2.31 GiB anon, 1.66 GiB file, `memory.current` 4.00 GiB |
| threads | 76 at a fresh start, 99 under load, stable there |
| VRAM | 9558 MiB held by the daemon |
| attribution | 118 of 120 hits came from member repos, 2 from the root itself |

The daemon is `Nice=10 CPUWeight=20 IOWeight=20`, so the one core it uses is the one nothing else
wanted.

# `memory.events high` is the wrong thing to assert

Four plans in a row wrote `high == 0` into a verification block. It reads as "the limit was never
touched" and it means something else. `high` counts *reclaim passes*, and 292 sqlite stores read
off disk put 1.66 GiB of page cache inside the cgroup. Page cache is what reclaim is for.

The cost of all 404 of them, from `memory.pressure`: **26.8 ms of stall, cumulative, over the
daemon's entire uptime**. Anon never went near the limit.

Assert `oom_kill` and `max` at zero, anon under the limit, and `memory.pressure` flat. A run that
fails on `high` fails on the kernel working.

# What this does not say

`the-cpu-side-of-an-indexing-pass-is-already-flat` measured the indexing side and found no arm that
wins. This is the *serving* side, characterised so nobody measures it a third time, and it is not
evidence for an optimisation. The latency figure is from this host and inherits
`this-host-cannot-produce-an-admissible-latency-number`.

# 2026-09-03: the same root at 361 members, and where the 1.46 s went

The measurement above is kept as it was taken. That root now holds **361 projects in its search
unit** — its `repositories/` directory carries 451 symlinks, and `federation.unit` resolves them
to 360 enabled members plus the root. Same host, same daemon, from `searches.jsonl`:

| search unit | n | p50 |
|---|---|---|
| 1 to 5 projects | 6,526 | 179 ms |
| over 100 projects | 559 | 24,631 ms |

For that root alone: 451 searches, **162 of them over 30 s**, slowest 478.7 s. One client gave up
and logged `no response or progress notification for 329s (idle timeout 300s)`. Nothing errored.
The server was still working.

**A second concurrent search did not divide the work.** Two traces sent in parallel by the same
client finished 6 ms apart, at 407,231 ms and 407,238 ms. Each one was walking its own 361 stores
in its own sequential loop.

Two causes, and this card is the fan-out half. The other half is
[a vector table kept every block it ever allocated](../defects/a-vector-table-kept-every-block-it-ever-allocated.md),
which made every one of those 361 KNN reads about six times larger than the live data.

`search.py:242` now calls `conns.fanout`, which maps the per-project retrieval over a
process-wide `ThreadPoolExecutor` of `config.FANOUT_WORKERS` threads, 4 by default. Three
properties decide the shape:

- **The pool is module-level and built once.** `conns._caches` is a list that only grows, so a
  pool per search would register a `_Cache` per worker per search and leave every one behind.
  Reused, the handle set is bounded by `FANOUT_WORKERS x unit` and not by `THREAD_LIMIT x unit`:
  4 x 361 handles at 0.27 MiB is about 390 MiB, on the arithmetic in
  [a SQLite handle is not free at rest](a-sqlite-handle-is-not-free-at-rest.md).
- **The session is entered on the worker, never on the caller.** The lock `reap_idle` skips is per
  thread. Under a pool the dispatching thread opens no store, so a session held there guards
  nothing and the reap is free to close a store under a live cursor.
- **`Executor.map` yields in the order it was asked.** That is load-bearing, not incidental:
  `pool_cut` hands out its slots in the order the pool lists the members, so a fan-out returning
  results as they finished would reorder the answer by disk timing. The threaded pool is the same
  list the sequential loop built.

Four workers rather than eight because the cost is disk and handles, not CPU. `FANOUT_WORKERS=1`
restores the sequential loop.
