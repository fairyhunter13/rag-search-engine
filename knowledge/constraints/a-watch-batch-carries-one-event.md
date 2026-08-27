---
type: Constraint
resource: src/coderag/quiet.py, src/coderag/index.py, src/coderag/watch.py, src/coderag/config.py
title: A watch batch carries one event, and each one paid for a whole walk
description: "The watcher submitted per batch and a batch is one save, so an editor writing through a build cost 303 index passes in 15 minutes. A pass is a full content-hash walk whatever moved. A 15 s per-project quiet window merges them, and it trades away no more freshness than the window."
tags: [indexing, watch, cpu, scheduler]
status: stable
generated: { by: claude/opus-5, at: 2026-08-28T00:00:00Z }
---

# What the ledger showed

`runs.jsonl` recorded **2,085 index passes in one hour** on a 412-project fleet. **303 of them fell
in 15 minutes.** Every row read `reason: watch`, `paths: 1` and `queued_ms: 0.19`. So the batch that
woke the indexer named a single file, and it waited for nothing.

The cost is not the file. `index_project` hashes every tracked file to find what moved, so a
one-line save on the largest project in the fleet walks 10,408 files. The walk is the pass.

Debouncing harder in the watcher does not reach this. `WATCH_DEBOUNCE_MS` is the Rust batch window
inside `watchfiles`, and these events arrive 5 to 20 seconds apart. A batch window wide enough to
merge them stops feeling immediate for the save that is genuinely alone.

# The window, and what it costs

`quiet.py` holds a job per project. A further event widens the held paths and restarts the
countdown. The pass then runs once the project stops changing, rather than once per save. The window
is `WATCH_QUIET_MS`, 15,000 ms, matched to the same figure in `graphrag`.

The freshness given up is exactly the window. A file saved and then left alone is searchable about
15 s later instead of about 1 s later. During an active edit it is longer, because each save
restarts the countdown. That is the point: nobody searches for the intermediate states.

Nothing else waits. An explicit `index` call submits with no delay, and it **takes the held job with
it**. So the pass it runs is never narrower than the one it displaced. `WATCH_QUIET_MS=0` restores
the per-batch pass.

# A held job has to be visible, and losing one has to be safe

The hold is off the queue, so `queue_depth` cannot see it. `index.status()` publishes `held`, and
`tools._pending` counts a held project for its unit. Without that second one, `index` answers 0 to
"I saved a file and nothing happened". That is the reading the surface exists to prevent.

A shutdown drops whatever is held. That is safe here and only here. Staleness is one content-hash
diff, so the hourly reconcile finds the same change without knowing an event was missed.

# Why this came up

The idle half of `conns.reap_idle` had never fired on the live daemon. It closes a thread's whole
handle set only after `STORE_IDLE_S`, 600 s. The indexer thread was never quiet that long at 303
passes per 15 minutes. Measured over one 1,400 s window with the daemon otherwise unused: 1,291 to
2,065 open `index.db` handles, and no reap. See
[a SQLite handle is not free at rest](a-sqlite-handle-is-not-free-at-rest.md).
