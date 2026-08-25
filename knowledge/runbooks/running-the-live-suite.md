---
type: Runbook
resource: tests/eval.py, src/coderag/gpu.py, src/coderag/server.py, tests/test_restart.py
title: Running anything that touches the real GPU
description: Two preconditions, a clear lock and ≥10 GiB free VRAM. Also the one-at-a-time rule, and the switch that separates starting the daemon from starting an overnight fleet index.
tags: [gpu, testing, operations]
status: stable
generated: { by: claude/opus-5, at: 2026-08-19T10:25:00Z }
---

# Before you start

**Both preconditions, or the run is invalid.**

1. **A clear lock.** Only one live suite runs at a time on this machine. Editing parallelises
   across sessions. Live testing does not. Check for another run first — `pgrep -af tests/eval.py`
   and the daemon's own state — and **refuse rather than interleave**. Two runs sharing a 16 GB card
do not fail cleanly. They produce numbers, and the numbers are wrong.
2. **≥10 GiB free VRAM.** Not "the daemon says it released" — the freed number. The old engine's
   `/api/gpu/release` returned 200 without freeing anything, so a check on its status code passed
   over a full card. Read `nvidia-smi`.

# Starting the daemon is not the same act as indexing the fleet

`lifespan` sweeps every enabled project into the queue at startup. So `systemctl --user start
coderag` on a registry of 148 rows *is* the fleet index. That is how the fleet index is meant to
be run. It is also why the live suite could not have a daemon up without one.
`CODERAG_RECONCILE_ON_START=0` starts a daemon that serves and watches but sweeps nothing.

The 60 s tick still picks up anything submitted, so a live test that submits its own project is
unaffected. Use it for the live suite and for any bake-off that needs the card. Leave it unset to
index the fleet.

# Running it

Each arm is its own subprocess with its own `STATE_DIR`. That is not isolation theatre. Every knob
an arm varies is a module-level constant read from the environment at import. So two arms in one
process measure whichever one imported first. It is also the safe shape. An arm that builds a
store in-process is one mistake away from building it in the real registry, which has occurred.

Long runs go to a file and get checked, not watched. One arm on a 7,000-file repo is ~28 minutes.
Backgrounding a `nohup … &` *inside* a backgrounded call makes the launcher exit immediately and
report success. The work is still running. Confirm with `pgrep -af tests/eval.py` before believing
any completion notice.

# Reading the result

**Read the deltas, not the levels**. Absolute recall collapses with store size from distractor
count alone. The same model measured 0.61 on a 1.3k-chunk store and 0.19 on a 21k-chunk one. Only
compare arms in a run.

`--lane dense` is the default for a reason stated in `tests/eval.py`. An arm that fails to load is
a **result**, not a bug to fix. A model that does not run in this stack is a model
that does not ship.

# After

The GPU does not come back on its own. `release_models()` and the idle timer exist because an idle
daemon was measured holding 12.2 GB with 3.5 GB free. If the next thing you do needs the card, stop
the daemon rather than trusting the timer's window.

# Pruning after a run

Every run that registers a tmp project and releases it leaves a store with no row. Finish with
`coderag doctor` to see them and `coderag doctor --prune` to delete them. Prune is the only
destructive subcommand. It holds the registry lock and keeps anything written to in the last 60s,
so it is safe to run with the daemon up. The rule it does **not** break is disable-never-prune:
rows are never touched, only stores nothing claims.
