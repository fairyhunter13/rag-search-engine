---
type: Decision
resource: tests/cpu.py, src/coderag/embed.py
title: jemalloc is refused, because three arms lost and the last one was worse than glibc
description: "Swapped in by LD_PRELOAD against a gate of -25% RssAnon written before any run. The subprocess arm returned -5.3%. The live daemon returned -12% at 40 minutes and +3.7% at 55, rising monotonically 2,053 -> 2,278 -> 2,682 MB against a 2,586 MB glibc baseline, so the arm ended worse than what it replaces. The mechanism is workload shape: the published wins come from high-churn small-object services, and this daemon pins two ONNX models. Two traps recorded with it -- malloc_trim under jemalloc does nothing, which would have silently broken release_models, and jemalloc is maintained again as of March 2026 so nobody should re-open this on that argument."
tags: [memory, allocator, measurement, kill-criterion]
status: stable
generated: { by: claude/opus-5, at: 2026-09-04T14:00:00Z }
sources:
  - id: cpu-harness
    resource: tests/cpu.py
  - id: release-models
    resource: src/coderag/embed.py
---

# Decision

The daemon keeps glibc malloc. jemalloc is not adopted, and the `+jemalloc` arm in `tests/cpu.py`
stays where it is with its failing threshold unchanged.

# The three arms

The gate was **−25% `RssAnon`**, written before any run.

| arm | what it measured | result | verdict |
|---|---|---|---|
| subprocess | three 287-chunk passes, own models | −5.3% | fail |
| live daemon | 40 minutes | −12% | fail |
| live daemon | 55 minutes | **+3.7%** | worse than glibc |

The live figure is 2,682 MB against a 2,586 MB glibc baseline, and the trajectory rises the whole
way: 2,053 → 2,278 → 2,682 MB. jemalloc was confirmed resident throughout, with five entries in
the process's own `maps`, so the experiment ran and lost rather than failing to start.

The subprocess arm is not the question and never was. It measures three passes over 287 chunks,
and the target was a daemon holding gigabytes after hours. The daemon arm is the one that decides,
and it decided against.

# Why, so the next reader does not re-run it

The published 52–67% figures come from high-churn, small-object services, where the prize is
glibc's arena fragmentation. This daemon pins two ONNX models and reads SQLite. Its large
allocations are the ones glibc already serves from `mmap` and returns on free, so there is little
fragmentation left for an allocator swap to reclaim. Swapping the allocator does not change the
shape of the workload, and the shape is the whole reason the published numbers are large.

# Two traps, both of which cut against re-opening this

**`malloc_trim` under jemalloc does nothing.** `embed.release_models` calls
`ctypes.CDLL("libc.so.6").malloc_trim(0)`, and it returns 2,082 MiB of 2,322 MiB. Under an
`LD_PRELOAD`ed jemalloc that call is inert. No release event fired in the 55-minute window, so the
experiment never exposed it — adopting jemalloc on the RSS figure alone would have taken the
largest single reclaim in this engine away silently.

**jemalloc is maintained.** Meta un-archived it in March 2026. The refusal here is about workload
shape, so a future reader must not read it as a maintenance judgement and must not re-open it when
the maintenance story improves further.

# What reverses this

A workload in this daemon that is small-object and high-churn, measured rather than assumed, and a
daemon-level arm that clears the −25% gate over a window long enough to show a trajectory rather
than a point.

`enable_cpu_mem_arena=False` is the other allocator-shaped suggestion and it is already refused in
[the CPU side of an indexing pass is already flat](../constraints/the-cpu-side-of-an-indexing-pass-is-already-flat.md).
So is `MALLOC_ARENA_MAX`, which cost 27% more CPU. This decision is not those two under a new name,
and it does not reopen either.
