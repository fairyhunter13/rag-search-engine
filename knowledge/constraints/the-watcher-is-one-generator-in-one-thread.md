---
type: Constraint
resource: src/rag_search/daemon/watcher.py
title: The watcher is one generator in one thread
description: HR37 and HR33 — a single `watchfiles.watch()` covers every root, filtering and coalescing happen in Rust before Python sees an event, and a dynamic root add restarts the generator through an acknowledged handshake.
tags: [watcher, inotify, watchfiles, hr33, hr37]
status: active
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# The watcher is one generator in one thread

`Watcher.start()` runs exactly one `watchfiles.watch()` generator, covering **all** registered
roots, in a single `rse-watcher` thread.

## Why one, counted

The previous implementation was a watchdog `Observer`, which allocates one inotify instance plus an
emitter and buffer thread **per scheduled watch**. On a 139-member federation that is roughly 278
threads and 139 inotify instances — already past the default
`fs.inotify.max_user_instances=128`.

It was also the fourth independent idle-CPU cause found on 2026-07-01, and the most surprising one:
a live `py-spy dump` showed the daemon's watcher dispatch thread pinned at ~104 % CPU for the full
64-minute process life, RSS 2.45 GB in an unbounded Python-side `InotifyBuffer`/`DelayedQueue` —
**even though the drift gates were correctly reporting "no drift"** for the same ignored-dir churn.
The gates were right. The machinery asking them, once per raw kernel event, was the burn.

That is the reusable shape: a correct gate downstream does not make an unbounded event source
cheap.

## Filtering happens before Python

`_filter(change, path)` finds the owning root via `_owning_root` and reuses the same
`is_ignored_path` resolver the drift gate uses — see
[one predicate decides what is indexed](one-predicate-decides-what-is-indexed.md). It is passed as
`watch()`'s `watch_filter` callable, so ignored-dir events are dropped and never reach `on_change`.

`debounce` and `step`, both Rust-side, coalesce a burst into one batch per loop iteration. The batch
is grouped by owning root and dispatched as one `on_change(root, files)` per root per batch, which
subsumes the old hand-rolled per-project debounce.

`_owning_root` is a boundary-aware longest-`Path.relative_to` match, never a `startswith` — see
[the watcher attributed events by string prefix](../defects/the-watcher-attributed-events-by-string-prefix.md).

## The dynamic-add handshake

`watch()` cannot take new paths while running, so adding a root sets a `_restart`
`threading.Event`. `_StopOrRestart.is_set()` makes the current generator return `'stop'`, and the
outer loop relaunches with the updated root set.

The subtle part is the acknowledgement. `watch()` blocks on a bounded `_restart_ack` event — set by
the loop immediately after it clears `_restart` and is about to re-enter `_watch()` — before
returning to the caller. Without it, a write landing in the teardown/rearm gap is **silently lost**:
`notify` does not retroactively see events from before a watch is armed. This race was found during
test-writing, not in production, which is the only reason it is cheap to know about.

## The fallback that is deliberately not hand-rolled (HR33)

If polling is ever needed — NFS, SMB, WSL — it is the Rust `notify` crate's own `force_polling`,
never a Python loop. `watch()`'s `Change` enum has only added/modified/deleted; there are no
close/open metadata events to filter, so any "filter the noisy events" design has nothing to filter.
fanotify would require root, so a user-service daemon stays on inotify via the Rust library.

## Sources

Rows HR33 and HR37 in [§13b](../../docs/architecture/federation-ops-and-invariants.md).

| Row | Guard | File |
|---|---|---|
| HR33 | `test_watcher_prefers_inotify_over_poll` | `test_idle_stability.py` |
| HR37 | `test_wt1_ignored_dir_churn_never_reaches_on_change`, `test_wt2_real_edit_fires_once`, `test_wt3_batch_coalescing_single_call_per_burst`, `test_wt5_prefix_sibling_roots_no_misattribution` | `test_idle_stability.py` |

Incident record: [idle CPU root causes](../../docs/decisions/2026-07-01-idle-cpu-root-causes.md).
