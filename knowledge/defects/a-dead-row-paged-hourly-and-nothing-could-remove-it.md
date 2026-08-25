---
type: Defect
resource: src/coderag/registry.py, src/coderag/cli.py
title: A dead row paged hourly, and no command could remove it
description: Twenty rows pointing at deleted temp directories re-failed on every sweep, so the two-sample rule pages forever. `doctor` named them, `--prune` reached only stores, and the recorded fix was editing projects.json by hand. `HEALTH_FAILING_CAP` is 20, so the alert was saturated with junk and could not have shown a real failure.
tags: [registry, alerting, pruning, resolved]
status: stable
generated: { by: claude/opus-5, at: 2026-08-24T00:00:00Z }
---

# The loop that could not be exited

A session running in a temp directory reads its host's SessionStart card. The card says the
directory is in no indexed project, and tells the session to call `index`. It does. The directory
is deleted when the run ends.

`index_project` then raises `FileNotFoundError` for it, and `_drain` records the error.
`reconcile_all` re-enqueues every enabled row on the hourly sweep. So the row fails at every
check, clears both samples of [the two-sample
rule](../constraints/a-fleet-alert-decides-on-two-samples-not-on-a-count.md), and pages every hour
for as long as the row exists.

Nothing could end it. `doctor` printed `MISSING` and counted a problem. `--prune` walked only
unclaimed store directories. `log.md` records the previous occurrence being cleared by "releasing
both dead rows by hand", which is not a procedure — it is the absence of one.

The second-order harm is the one worth keeping: `HEALTH_FAILING_CAP` is 20 and the failing list had
reached exactly 20. The alert was full. A real project failing at that moment would have been
invisible in the very message sent to say something was wrong.

# Why the fix is a key list and not a predicate

`forget(keys)` removes named rows and nothing else, and it never reads the disk. That is the
explicit-removal rule `registry.py`'s module docstring states, and the reason for it is recorded
there. The version of `load()` that pruned on a missing path wiped 236 rows when a caller ran it
in-process against the real state. Three things are indistinguishable from a deletion at the
moment of the read. They are an unmounted volume, a repo moved for ten seconds, and a member
behind a broken symlink.

So the judgement lives in `doctor --prune`, where a human invoked it, and `forget` only executes it.

One `_mutate()` write for the whole set, which is not tidiness. `_rotate_backup` stamps to the
second: twenty per-row `release()` calls overwrite one backup, and what survives as the restore
point is a half-pruned registry.

# The gate is the claim, not the error

`--prune` keeps a missing row when a root that still names it exists on disk. That claim is another
project's configuration, and the member is behind a broken symlink or an unmounted volume rather
than gone.

It is deliberately not gated on `last_error`. The hourly sweep clears `last_error` on the next
success. So a rule that reads it makes `--prune` depend on where in the hour it was run. That is
the same property that forced the alert onto two samples instead of one field.

# Silencing the card did not stop it, because the engine says the same thing

The host's SessionStart card was made silent under `TMPDIR` and a live run afterwards still
produced ten new rows. The transcripts name the surviving source: `search` refuses an unindexed
root with *"call index(root='/tmp/…') first"*, and the model does exactly that. The card was one
of two mouths.

The engine cannot close the other one. Refusing a temp root — in `search`'s message, in
`tools.index_project`, anywhere — reds this repo's own suite, because every fixture project is a
`tmp_path` directory. A throwaway agent workdir and a test fixture are the same object, and no
predicate here separates them.

What does separate them is the caller. Whoever creates a directory it will delete knows that at
creation time. `coderag forget <path>` exists for it to say so. The harness pairs the two:
`internal/livecap/runner.go` forgets the workdir in the same defer that removes it. The row is
gone while the directory still exists, which is the only moment at which removing it is
unambiguous. `--prune` can act only afterwards, and afterwards is when the page has already fired.

# The freed store leaves the way every other store leaves

The forget runs before the store walk, so a row's directory becomes unclaimed and exits through
`prunable_stores()`, which already holds the lock and honours `PRUNE_MIN_IDLE_S`. No second
`rmtree` over a calculated set. Both fleet-wide index wipes in this engine's history came from
one, and [prune racing the daemon](prune-raced-a-store-the-daemon-was-writing.md) is the same
lesson from the other direction. The idle-gate arm of `test_prune_forgets_a_row_whose_directory_is_gone` is what
reds if anyone adds one back.
