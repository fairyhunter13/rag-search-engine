---
type: Defect
resource: src/coderag/watch.py, src/coderag/health.py, src/coderag/server.py, tests/test_watch.py, tests/test_health.py
title: The watcher went blind in two ways, and reported neither
description: "A project dropped for a broken config never re-armed, because the repair moves no registry row and the row was the whole comparison. And an `OSError` out of inotify killed the thread, where nothing wraps it. Both read as healthy at every layer that was asked."
tags: [watcher, inotify, observability, health]
status: stable
generated: { by: claude/opus-5, at: 2026-08-25T15:30:00Z }
---

# The repair was invisible to the thing that watches for repairs

The loop drops a project whose `.coderag.yaml` does not parse, and keeps the fleet. That much is
right. The comment beside it recorded the rest: fixing the file alters no registry row, so
`rearm_if_changed` never fires on it again.

`_intent` held the enabled paths. Repairing a config changes no path. So the comparison could not
see the one event it exists to see, and that project stayed unwatched until the daemon restarted.

Nothing else notices. The hourly reconcile keeps the store fresh, so a search still answers. Only a
write between two sweeps is lost, which is the case a watcher exists for.

`_intent` now pairs each path with the mtimes of `.coderag.yaml` and `.coderag.toml`. Both files,
because both drop the project: `projcfg` refuses a leftover TOML file rather than ignoring it, and
deleting it is the repair. A missing file stamps 0.0, so writing one moves the stamp too.

Only the project's own pair. `projcfg.effective` reads a claiming root's config as well, and
suppresses a broken one there. A root's typo raises for the root, never for the member.

# The other way was louder and reported even less

`_loop` caught `projcfg.ConfigError` and nothing else. `_watch` is where inotify raises at the
per-user watch ceiling, and where a project deleted mid-pass raises. Uncaught, that killed the
thread.

`server._guarded` does not cover it. It wraps the scheduler thread, and this is a different one.
So the recovery was the 60 s respawn, every event in the window was lost with no replay, and
`scheduler_errors` carried nothing.

The loop now catches `OSError`, records it in `watch.error()`, clears `_armed` and re-arms on the
next tick. `/healthz` publishes that under `scheduler_errors` as `watch`, which puts it in the
existing two-sample rule with no new alerting path.

Headroom is not the point today: 101,767 watches against a ceiling of 1,048,576, which is 9.7%.
This is the guard for the day that changes.

# What the checker was reading while both of these were true

Two fields, `failing` and `scheduler_errors`. `/healthz` publishes twelve. So a dead watcher passed
every check ever run against it. The alert built to find this class of failure could not see either
defect above.

The checker now reads `watching` and the queue depth as well.
[A fleet alert decides on two samples](../constraints/a-fleet-alert-decides-on-two-samples-not-on-a-count.md)
carries that rule and its amendment.
