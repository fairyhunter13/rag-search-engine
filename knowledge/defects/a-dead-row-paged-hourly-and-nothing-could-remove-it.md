---
type: Defect
resource: src/coderag/registry.py, src/coderag/cli.py
title: A dead row paged hourly, and no command could remove it
description: "Twenty rows pointing at deleted temp directories re-failed on every sweep, so the two-sample alert rule pages forever. `doctor` named them, `--prune` reached only stores, and the recorded fix was editing projects.json by hand — while `HEALTH_FAILING_CAP` is 20, so the alert was saturated with junk and could not have shown a real failure."
tags: [registry, alerting, pruning, resolved]
status: stable
generated: { by: claude/opus-5, at: 2026-08-24T00:00:00Z }
---

# The loop that could not be exited

A session running in a temp directory is told by its host's SessionStart card that the directory is
in no indexed project and to call `index`. It does. The directory is deleted when the run ends.
`index_project` then raises `FileNotFoundError` for it, `_drain` records the error, and
`reconcile_all` re-enqueues every enabled row on the hourly sweep — so the row fails at every
check, clears both samples of
[the two-sample rule](../constraints/a-fleet-alert-decides-on-two-samples-not-on-a-count.md), and
pages every hour for as long as the row exists.

Nothing could end it. `doctor` printed `MISSING` and counted a problem; `--prune` walked only
unclaimed store directories. `log.md` records the previous occurrence being cleared by "releasing
both dead rows by hand", which is not a procedure — it is the absence of one.

The second-order harm is the one worth keeping: `HEALTH_FAILING_CAP` is 20 and the failing list had
reached exactly 20. The alert was full. A real project failing at that moment would have been
invisible in the very message sent to say something was wrong.

# Why the fix is a key list and not a predicate

`forget(keys)` removes named rows and nothing else, and it never reads the disk. That is the explicit-removal rule
`registry.py`'s module docstring states, and the reason for it is recorded there: the version of `load()` that pruned on a missing path wiped 236 rows when a caller ran it
in-process against the real state. An unmounted volume, a repo moved for ten seconds and a member
behind a broken symlink are all indistinguishable from a deletion at the moment of the read.

So the judgement lives in `doctor --prune`, where a human invoked it, and `forget` only executes it.

One `_mutate()` write for the whole set, which is not tidiness. `_rotate_backup` stamps to the
second: twenty per-row `release()` calls overwrite one backup, and what survives as the restore
point is a half-pruned registry.

# The gate is the claim, not the error

`--prune` keeps a missing row when a root that still names it exists on disk. That claim is another
project's configuration, and the member is behind a broken symlink or an unmounted volume rather
than gone.

It is deliberately not gated on `last_error`. The hourly sweep clears `last_error` on the next
success, so a rule that reads it makes `--prune` depend on where in the hour it was run — the same
property that forced the alert onto two samples instead of one field.

# The freed store leaves the way every other store leaves

The forget runs before the store walk, so a row's directory becomes unclaimed and exits through
`prunable_stores()`, which already holds the lock and honours `PRUNE_MIN_IDLE_S`. No second
`rmtree` over a computed set: both fleet-wide index wipes in this engine's history came from one,
and [prune racing the daemon](prune-raced-a-store-the-daemon-was-writing.md) is the same lesson from
the other direction. The idle-gate arm of `test_prune_forgets_a_row_whose_directory_is_gone` is what
reds if anyone adds one back.
