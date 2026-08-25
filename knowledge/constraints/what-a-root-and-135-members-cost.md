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
