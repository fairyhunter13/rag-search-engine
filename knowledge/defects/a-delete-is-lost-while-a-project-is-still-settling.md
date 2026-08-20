---
type: Defect
resource: src/coderag/watch.py, src/coderag/index.py, tests/test_live_results.py
title: A delete is lost while a project is still settling, and delivered in 10 s once it has
description: "The same tree, the same shape, the same fleet: a file deleted shortly after the project was registered stayed searchable past 300 s, and one deleted after a 90 s quiet period left the store in 10 s. The settling window was the probe's own doing -- the `index` tool re-armed every inotify watch on every call, and the test polled it."
tags: [watcher, inotify, federation, indexer, resolved]
status: resolved
generated: { by: claude/opus-5, at: 2026-08-20T16:40:00Z }
---

# What reproduces, and what does not

`test_a_deleted_file_stops_being_returned` fails: a file removed from a federation member reached
only through a directory symlink is still returned by `search` 300 s later, and the member's own
store still holds the row. It is the only red test in the live lane and it is not caused by the
scoping change shipped alongside it — journald shows `workspace pin: 0 root(s)` on every call in the
window and no refusals.

Two probes, the same tree shape, the same fleet of ~155 watched projects, differing only in when the
delete happens:

| probe | delete issued | result |
|---|---|---|
| register, index, write, delete | ~60 s after registration | **still searchable after 300 s**, store unchanged |
| register, index, wait 90 s quiet, write, delete | after the member had been idle 90 s | create delivered, **delete delivered at t+10 s** |

The failing live test has the first shape: a module-scoped fixture registers the tree, and the write
and delete tests run back to back behind it.

# What is ruled out, by construction rather than by argument

Each of these was a live hypothesis and each is now dead:

- **notify does not report deletes.** It does. A raw `watch()` on a root plus its symlinked member
  reports `Change.deleted` at the member's *real* path in 1.1 s, and at 151 roots a delete is
  reported at every lead time out to 90 s.
- **`_dispatch` drops the deleted path.** It does not.
  `test_a_delete_through_a_symlink_still_reaches_the_queue` drives real inotify, deletes through the
  link while the watcher watches only the real path, and asserts the job names the resolved path.
  The file is created *before* the watcher starts, so the only event it can pass on is the removal.
- **`index_project`'s scoped branch does not delete.** It does.
  `test_a_scoped_pass_removes_the_file_the_watcher_named` calls it the way the watcher does — with
  `paths`, so the content-hash diff never runs and the delete list is computed from the named paths
  alone. The pre-existing test only ever exercised the full-walk branch.
- **The indexer is backed up.** It is not: `queue_depth: 0`, `state: idle`, 1,360 passes completed.
- **The watcher is wedged or blind.** It is not. `py-spy` puts the watcher thread — the original one
  from daemon start — idle inside `watch()`, and the daemon logged 382 event batches in three
  minutes during the window.
- **`/healthz`'s `completed` counter shows the pass ran.** It shows nothing of the kind, and an
  earlier reading of this defect rested on it. The counter is fleet-wide across ~155 watched
  projects, so a `+1` is not attributable to the tree under test. Per-project attribution has to come
  from the project's own store or its own registry row.

# The two ways a probe here lies

Both were made, and both inverted the answer:

- **Polling `index` repairs what it measures.** The tool submits a full pass, so a search-and-`index`
  poll loop converges in 20 s and reports the bug fixed. The store has to be read read-only off its
  own sqlite file and the only daemon call in the loop can be `search`.
- **The member's first pass covers the write.** The fixture waits on the *root's* chunk count and
  then writes into the *member*, whose own walk has not run yet and sweeps the new file in. So
  `test_a_write_reached_only_through_a_symlink_is_noticed` — written the awkward way on purpose —
  can still pass without the watcher ever firing. Waiting for the member's store to go quiet first is
  what makes the write assertion mean what it says.

# The mechanism: the probe was blinding the watcher

`tools.index_project` called `watch.rearm()` unconditionally, on **every** `index` call, registry
change or not. Re-arming tears down every inotify watch and rebuilds it — ~120,000 of them on this
fleet, 5.4 s measured over 151 projects — and the loop only notices the flag after up to
`WATCH_POLL_MS` (5 s). So each `index` call opened a blind window of up to ~10.4 s, and inotify
replays nothing that falls inside one.

Everything the two probes disagreed on follows from that. The failing test polls `index` while it
waits, so the window was open more or less continuously and the delete never landed; the 90 s-idle
probe made no `index` call in its window, so the delete arrived in 10 s. Three earlier sessions
looked at the delivery path because every layer of it *was* correct — the events were being dropped
before any of it ran.

The fix is that `rearm_if_changed` — which compares the watch set and existed already — is now the
only entry point, and `rearm` is deleted. `test_the_index_tool_does_not_rearm_a_registry_it_did_not_change`
asserts both halves: a repeat call must not re-arm, and a call that registers something must.
`tests/test_live_results.py` now runs to green in 12 s from a cold registration, where it timed out
at 300 s.
