---
type: Defect
resource: src/coderag/watch.py, src/coderag/watchchild.py, tests/test_watch.py
title: Arming the watch held the GIL past the watchdog, and systemd killed the daemon 7 times
description: "watchfiles' `RustNotify` holds the GIL while its inotify thread walks every directory under every root. 110,827 directories held it for 7.7 s at load 12, and past 90 s at load 27-40, so no watchdog ping went out and each restart walked again. The watch now arms in a child process."
tags: [watcher, availability, watchdog, gil, resolved]
status: stable
generated: { by: claude/opus-5, at: 2026-09-25T20:05:00+07:00 }
---

# The symptom

Between 19:22 and 19:33 on 2026-09-25, systemd killed coderag 7 times with `Watchdog timeout
(limit 1min 30s)`. There had been no watchdog kill in the 7 days before. The daemon logged no
"withholding the watchdog ping" line, and the pinger's locals read `why: ""` and `interval: 30`.

# The cause

The apport core of the kill at 19:24:41 names it. Thread 18 held the GIL in
`RustNotify.__new__`, waiting on a channel for `notify`'s event-loop thread. That thread was in
`add_watch` → `walkdir`, walking all 451 projects. The main thread, the pinger and two workers
waited for the GIL, so no ping could be sent. Measured standalone, arming took 7.8 s at load 12,
and a 10 ms heartbeat thread saw one gap of 7.7 s. Under load 27-40 the walk outran 90 s. 55% of
the 110,827 directories sit under names this engine never indexes, `node_modules` and `.git`
first.

watchfiles 1.3.0 does not release the GIL there, and agentscope-ai/QwenPaw#7725 reports the same
hold, 25 s over 192,000 files.

# The fix

`_watch` runs `watchfiles.watch` in `python -m coderag.watchchild` and yields the same batches,
read from a pipe with `select` every 0.2 s. The walk holds the child's GIL. The child writes an
empty batch on every timeout, so `armed` still flips on the first yield. A dead child raises
`OSError`, which `_loop` already re-arms on. Over 60,000 directories the parent's heartbeat gap
is 0.01 s, against 0.55-0.62 s in-process.

Pruning the walk would shorten the stall and keep it, and a longer `WatchdogSec` would hide it.
Neither fixes the cause.
