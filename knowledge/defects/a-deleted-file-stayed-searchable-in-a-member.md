---
type: Defect
resource: src/coderag/watch.py, src/coderag/server.py, src/coderag/config.py, tests/test_watch.py
title: A deleted file stayed searchable in a federated member, from two independent causes
description: "inotify keys a directory by inode and reports the last-registered path, so a member's writes were billed to its root; and the 60 s tick re-armed the whole watch set unconditionally, which is a 5.4 s blind window with no replay."
tags: [watcher, inotify, federation, symlink]
status: stable
generated: { by: claude/opus-5, at: 2026-08-20T02:00:00Z }
---

# What happened

Delete a file inside a federation member, wait through three 60 s polls, search from the root: the
file was still returned. Both halves of the watcher reported themselves healthy throughout —
`watching()` true, `watching 151 projects` in the log every minute.

Two defects, found separately, each sufficient on its own.

**One: the reported path is not the path the file lives at.** inotify identifies a directory by
inode. A root and the member it reaches through a directory symlink are the same inode, so they
collapse to one watch descriptor, and notify-rs reports events under whichever path was registered
**last** — measured both ways, and nothing in this code chooses the order. Unresolved, a member's
own write arrives as `root/link/src/x.py`, `_owner` bills it to the root, and the member's store is
never corrected. The fix is one `Path(raw).resolve()` in `_dispatch`.

The module docstring already carried the *other* symlink trap — inotify does not traverse a
symlinked directory — and registering members under their resolved path is what sidesteps it. That
was true and it is not this. Watching two paths that reach one inode is the case the resolved-path
rule does not cover.

**Two: the tick re-armed a watch set that had not changed.** `server._tick` called `watch.rearm()`
every 60 s unconditionally. A re-arm tears down every inotify watch and rebuilds it: **5.4 s over
151 projects and ~120,000 watches**, measured. So the watcher was blind for about a tenth of its
life, on a fixed schedule — and **inotify has no replay**. An event lost in that window is lost
permanently; nothing rescans. `rearm_if_changed()` compares the registry against what is armed and
sets the flag only on a difference.

# The general shape

A periodic refresh of a subscription is not free, and the cost is paid in the currency the
subscription exists to deliver. The window it opens is invisible in every health check, because
during it the watcher is not failing — it is simply not there.

Two rules fell out and are held by tests:

- The unfiltered root list is what gets compared. A project dropped from the walk for a broken
  `.coderag.toml` is still *enabled*, so comparing against the filtered list differs on every tick
  and re-arms forever — the first version of the fix did exactly that.
- Surfacing from Rust is free; re-arming is not. `WATCH_POLL_MS` (5 s) bounds how long a newly
  registered project waits to be noticed. It is a poll interval, not a re-arm interval, and the
  distinction is the whole fix.

# How the test discriminates

`test_the_tick_does_not_rearm_a_watch_set_that_has_not_changed` asserts both halves, because either
alone passes on broken code: an unconditional `rearm` passes the "new project is picked up" half,
and a `pass` passes the "unchanged set does not re-arm" half.

The routing test feeds `_dispatch` a synthetic batch rather than driving real notify. Driving it
through inotify would test which of the two paths happened to register last, which is the thing
nothing controls.

# Not the whole cause

The symptom came back — see
[a-delete-is-lost-while-a-project-is-still-settling](a-delete-is-lost-while-a-project-is-still-settling.md). Both fixes here still hold and both were
re-verified against real inotify: a delete through a symlink reaches the queue under the member's
resolved path, and the tick no longer re-arms an unchanged set. What they do not cover is a delete
issued while the project is still settling after registration, which is the case the live test has
and the one that is still open.
