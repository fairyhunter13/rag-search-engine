---
type: Decision
resource: src/coderag/quarantine.py, src/coderag/prune.py, src/coderag/disk.py, src/coderag/doctor.py
title: A dead row takes its store, and the delete is a move
description: Removing a row freed no disk at all -- the store stayed until a human typed a prune. The reaper now takes the store too, behind the idle floor, and it moves it into a week-long quarantine rather than deleting it.
tags: [registry, pruning, storage, fleet]
status: stable
generated: { by: claude/opus-5, at: 2026-08-31T00:00:00Z }
---

# The asymmetry this closes

[A row leaves on a delete event and never on a scan](a-row-leaves-on-a-delete-event-and-never-on-a-scan.md)
made the row automatic and left the bytes manual. The watcher dropped the row on the delete event;
the store directory stayed on disk, became *unclaimed*, and waited for someone to type
`coderag doctor --prune`. Measured on 2026-08-31: **197 of 616 store directories had no row, 748
MB**, and the structural engine held two more. Nothing in either engine returned a byte without a
human command.

`Pruner.run_due` now retires the store of every row it forgets. The trigger is unchanged — one
delete event, a parent test and a grace period — so this adds no predicate and no scan.

# Two guards

**The idle floor.** [A prune raced a store the daemon was writing](../defects/prune-raced-a-store-the-daemon-was-writing.md)
is recorded against the hand-typed prune, and moving the removal onto an automatic path is exactly
when it recurs: a job queued before its row was dropped still indexes into the directory, with no
row to hold it. `prune._is_idle` applies `PRUNE_MIN_IDLE_S` to the event path, which never went
through `prunable_stores` and so never saw that floor.

**The shape of the answer.** `disk.refuse_on_shape` refuses a verdict against an empty registry
outright, and one covering more than half the tree without `--force`. Both fleet-wide index wipes
returned a verdict shaped exactly like those, and neither was catchable by looking at any one store.
`--force` deliberately does not lift the empty-registry refusal: a registry that failed to load
looks identical to a fleet with nothing enrolled, and a human forcing a prune is answering "delete
these", not "the registry is empty on purpose". It sits in the shared walk, so a caller that never
reads `busy` still cannot act on a verdict covering the whole tree.

# Why a move and not a delete

Both wipes surfaced days late and the same way: someone searched a repository and got nothing back.
A store a person removed by hand has a witness at the moment it goes. One an automatic path removed
has none.

So the reaper renames the directory into `INDEX_DIR/.trash/<unix-ts>-<name>` and deletes it seven
days later. Three rules hold it:

- **A failed rename never becomes an `rmtree`.** The store stays where it stands and the caller
  reports a store it did not remove. A path whose whole purpose is undo cannot answer a failure by
  deleting harder.
- **`.trash` is not an unclaimed store.** No row names it, so the orphan walk would otherwise list
  it, delete it on the next pass, and report the undo as reclaimed waste.
- **A name the quarantine did not write is left alone.** Expiry parses its own timestamp prefix and
  skips anything else rather than guessing.

`coderag forget` quarantines too. It removed the row and left the whole store standing, which is the
same gap on the hand-typed surface.

# What this still does not clear

inotify has no replay, so a repository deleted while the daemon was down reaches no event. That is
reported and never acted on — see
[an absent directory is three answers, and only one is a deletion](an-absent-directory-is-three-answers-and-only-one-is-a-deletion.md).
