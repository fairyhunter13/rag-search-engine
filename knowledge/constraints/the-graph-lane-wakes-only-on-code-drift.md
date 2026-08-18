---
type: Constraint
resource: src/rag_search/daemon/sweeps.py
title: The graph lane wakes only on code drift
description: HR1, HR2, HR32 and HR38 — the watcher is the only steady-state trigger, the vector index updates on every event, and the expensive graph re-derive is gated behind a code-only fingerprint on its own 45 s debounce.
tags: [daemon, watcher, drift-gate, idle-cpu, hr1, hr2, hr32, hr38]
status: active
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# The graph lane wakes only on code drift

There are no timers. A file changes, the watcher fires, and everything downstream is a consequence
of that one event — no sweep timer, no periodic reconcile timer. Understanding the daemon means
understanding what happens between the event and the GPU going quiet again.

## Two lanes off one event

`on_change(root, files)` runs two things in a fixed order, and the order is the invariant:

1. **The index step** (`_index_files` / `_index_project`) — incremental embedding of the changed
   files. Runs **unconditionally**, above every gate.
2. **The graph lane** — symbol re-derive plus `_label_project`, on its own 45 s per-project
   debounce (`_LANE_DEBOUNCE_S`), and only if the code fingerprint moved.

Full `_index_project` — graph plus k-core — runs at first index and at reconcile, never in steady
state.

## Why the gate is code-only (HR38)

`_code_source_fingerprint(path)` walks `iter_files` filtered through
`is_code_language(detect_language(f))`. Docs, config and image edits move the file tree and do not
move this fingerprint, so they do not wake a graph re-derive. Since the graph lane is the whole of
the daemon's heavy work, **this gate is the main thing standing between non-code churn and a busy
GPU**.

The ordering in step 1 is what makes that safe rather than lossy: a docs edit is always re-embedded,
because the index step runs above the gate. GG4 in `test_docs_index.py` pins that ordering — and it
is worth knowing that GG4 once asserted it against a fingerprint function with no production caller
at all, which is why the row is written as an ordering claim rather than a presence claim.

## Why a failed pass must not stamp

`_last_labelled_sig[path]` is stamped only by a pass that **completed** (`_graph_lane_pass`). On
exception the file batch is pushed back into `_pending_graph_files` and left unstamped, so the pass
retries. Stamping on entry would make a crashed pass indistinguishable from a successful one and
lose the drift silently.

## Two things that are not obvious from the names

**Queue-empty is not idle.** The graph re-derive runs on a dedicated lane (`_graph_lane_cv`,
`_graph_lane_wanted`, `_graph_lane_busy`) so it never occupies a dispatch worker. Code that reads
`graph.db` immediately after `on_change` returns must call `_graph_lane_join()`; checking that the
work queue drained will read a half-derived graph.

**`_HEAVY_LOCK` must not leave.** It serialises every CPU-bound graph pass — extraction and
labelling — across the watcher's dispatch workers, the graph lane and reconcile, so at most one
heavy pass runs at a time. It is never held around index/embed or a GPU query, and moving it would
turn a bounded settle into an unbounded one.

## What idle actually costs

With no cascade the daemon reaches true idle and `_idle_unload` (`RSE_MODEL_IDLE_UNLOAD_S` = 300 s,
checked on a 60 s scheduler tick) nulls the embedder and reranker, runs `gc.collect` plus
`malloc_trim(0)`, and returns the ORT CUDA arena to the OS — still the only reliable RSS release
path. Target: under 1 % of one core at idle, a restart's settle bounded to about one core, RSS at
the Python + uvicorn + sqlite floor.

The daemon must **never** self-exit on idle. A prior `sys.exit(0)` ran in a scheduler thread, where
`SystemExit` is swallowed — the exit did nothing and looked like it worked.

Cooperative gates are not the last word; the kernel ceiling is
[the daemon lives inside one core](the-daemon-lives-inside-one-core.md).

## Sources

Rows HR1, HR2, HR32 and HR38 in
[§13b](../../docs/architecture/federation-ops-and-invariants.md).

| Row | Guard | File |
|---|---|---|
| HR1 | `test_p34_watcher_updates_vector_index` | `test_daemon.py` |
| HR2 | `test_watcher_labelling_e2e`, `test_on_change_wires_graph_labelling` | `test_daemon.py` |
| HR32 | `test_drift_gate_skips_labelling_when_sig_unchanged`, `test_drift_gate_triggers_labelling_when_sig_changes`, `test_heavy_lock_serializes_concurrent_passes` | `test_daemon.py` |
| HR38 | the FCG1–FCG4 family | `test_docs_index.py` |

Incident record: [idle CPU root causes](../../docs/decisions/2026-07-01-idle-cpu-root-causes.md),
which names four independent causes found in one evening.
