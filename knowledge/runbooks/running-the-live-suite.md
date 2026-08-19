---
type: Runbook
resource: tests/eval.py, src/coderag/gpu.py
title: Running anything that touches the real GPU
description: Two preconditions — a clear lock and ≥10 GiB free VRAM — plus the one-at-a-time rule, because editing parallelises across sessions and live testing does not.
tags: [gpu, testing, operations]
status: stable
generated: { by: claude/opus-5, at: 2026-08-19T10:25:00Z }
---

# Before you start

**Both preconditions, or the run is invalid.**

1. **A clear lock.** Only one live suite runs at a time on this machine. Editing parallelises
   across sessions; live testing does not. Check for another run first — `pgrep -af tests/eval.py`
   and the daemon's own state — and **refuse rather than interleave**. Two runs sharing a 16 GB card
   do not fail cleanly; they produce numbers, and the numbers are wrong.
2. **≥10 GiB free VRAM.** Not "the daemon says it released" — the freed number. The old engine's
   `/api/gpu/release` returned 200 without freeing anything, so a check on its status code passed
   over a full card. Read `nvidia-smi`.

# Running it

Each arm is its own subprocess with its own `STATE_DIR`. That is not isolation theatre: every knob
an arm varies is a module-level constant read from the environment at import, so two arms in one
process measure whichever one imported first. It is also the safe shape — an arm that builds a
store in-process is one mistake away from building it in the real registry, which has happened.

Long runs go to a file and get checked, not watched. One arm on a 7,000-file repo is ~28 minutes.
Backgrounding a `nohup … &` *inside* a backgrounded call makes the launcher exit immediately and
report success; the work is still running. Confirm with `pgrep -af tests/eval.py` before believing
any completion notice.

# Reading the result

**Read the deltas, not the levels.** Absolute recall collapses with store size from distractor
count alone — the same model measured 0.61 on a 1.3k-chunk store and 0.19 on a 21k-chunk one. Only
compare arms within a run.

`--lane dense` is the default for a reason stated in `tests/eval.py`, and an arm that fails to load
is a **result**, not a bug to go and fix: a model that does not run in this stack is a model that
does not ship.

# After

The GPU does not come back on its own. `release_models()` and the idle timer exist because an idle
daemon was measured holding 12.2 GB with 3.5 GB free; if the next thing you do needs the card, stop
the daemon rather than trusting the timer's window.
