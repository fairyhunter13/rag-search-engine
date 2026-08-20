---
type: Constraint
resource: tests/cpu.py, src/coderag/embed.py, src/coderag/systemd.py
title: The CPU side of an indexing pass is already flat, and six arms failed to move it
description: "Measured over three repetitions per arm on a quiet machine: an indexing pass costs ~1.1 mean cores, and none of MALLOC_ARENA_MAX, RAYON_NUM_THREADS, intra/inter-op thread caps, spin-wait or the CPU arena cleared its pre-committed threshold. MALLOC_ARENA_MAX=2 made it 27% worse."
tags: [performance, onnxruntime, indexer, refuted]
status: accepted
generated: { by: claude/opus-5, at: 2026-08-20T17:55:00Z }
---

# The numbers

287-chunk pass over this repo's own materialized corpus, warm-up discarded, store deleted before each
repetition, `store.close_all()` first — a cached sqlite handle keeps the unlinked file alive, and the
first run reported three 0.03 s passes over an index it never rebuilt. Medians of three:

| arm | wall s | CPU-s | peak threads | VmHWM MB | threshold | verdict |
|---|---|---|---|---|---|---|
| baseline | 2.82 | 3.12 | 72 | 1179 | — | — |
| `+malloc` `MALLOC_ARENA_MAX=2` | 2.94 | **3.97** | 72 | 1179 | VmHWM −150 MB | **worse**, +27% CPU |
| `+rayon` `RAYON_NUM_THREADS=4` | 2.76 | 2.93 | 56 | 1169 | CPU −15% | −6.1%, loses |
| `+threads` intra/inter=1 | 2.83 | 3.13 | 49 | 1177 | CPU −25% | −0%, loses |
| `+nospin` `allow_spinning=0` | 2.85 | 3.13 | 72 | 1183 | −10% beyond `+threads` | none, loses |
| `+arena` `enable_cpu_mem_arena=False` | 2.79 | 3.00 | 72 | 1166 | VmHWM −200 MB | −13 MB, loses |

Not void: the baseline's own three passes spread 0.09 CPU-s against a best inter-arm delta of 0.19.

# Why nothing was there to win

A pass burns ~1.1 mean cores. The 72 threads are real and two pools deep — `+rayon` removes 16 and
`+threads` removes 23 — but they are parked, not spinning, so removing them removes no CPU. This is a
GPU pipeline and the CPU side was never the cost.

`MALLOC_ARENA_MAX=2` is the one result worth carrying: it is the arm people reach for first, it is
free, and here it costs 27% more CPU at the same wall time and saves nothing at all.

# What was deleted, and what stayed

The `CODERAG_ORT_*` constants added to measure this are gone: a knob nobody sets is a liability with
a test-shaped hole in it. `tests/cpu.py` keeps only the env-only arms, so it still runs against a
future model or batch shape without carrying dead switches.

The systemd resource keys shipped anyway and are not affected by this. `Nice=10`, `CPUWeight=20` and
`IOWeight=20` are a policy statement — this daemon loses to the editor — not a performance claim, and
`MemoryHigh=4G` sits above the 2.14–2.29 GiB measured with both models resident.

# Re-measuring

The per-thread split the harness was built for did not survive contact: every thread's `comm` reads
`python`, so attribution collapses to one bucket and only `peak_threads` discriminates. Fix that
before drawing a conclusion about *which* pool costs anything. The load gate (1-minute average under
1.0) refused a run once already and was right to — a CPU number taken next to a compile is not a
number.
