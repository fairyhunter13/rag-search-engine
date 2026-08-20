---
type: Defect
resource: src/coderag/cli.py, src/coderag/registry.py, src/coderag/config.py
title: doctor --prune globbed fresh directories against a stale row snapshot
description: "`registry.load()` releases its lock before returning, so the claimed set was a snapshot and the glob after it was live: a project claimed in between read as unclaimed and its store was deleted under the daemon's open handle, which on Linux keeps committing into the unlinked inode and reports nothing."
tags: [registry, cli, concurrency, data-loss, resolved]
status: resolved
generated: { by: claude/opus-5, at: 2026-08-20T23:40:00Z }
---

# The order was wrong, not the idea

Walking stores instead of rows is right — that is what [doctor only walked the
registry](doctor-only-walked-the-registry.md) fixed. What it got wrong is that the two halves of
the comparison came from different instants. `load()` takes a shared lock, reads, and unlocks
before it returns; the `INDEX_DIR.glob` ran after. Any project claimed and indexed in that window
is absent from `claimed`, present in the glob, and deleted.

The deletion is silent, which is what makes it the worst of the six. `store.connect` caches its
handle thread-locally and never revalidates, so after `rmtree` the writes keep succeeding into the
unlinked inode, `commit()` returns clean, and `registry.update` records chunk counts for a store
that no longer exists. The next `connect` creates an empty database. Search then answers zero hits
against a row claiming N chunks, with no error at any layer.

# One locked view, and an idleness floor for what the lock cannot cover

`registry._held(mode)` yields the rows with the lock **still held**, so `unclaimed_stores()` reads
rows and globs inside one view, and `prunable_stores()` does the same under `LOCK_EX` — nothing can
claim a candidate between the walk that names it and the `rmtree` that removes it.

The lock does not close the whole gap. A job queued before its row was deleted still creates its
directory, indexes into it and calls `registry.update`, which no-ops on a vanished row — a store
with no row that is not garbage. So a candidate written to within `CODERAG_PRUNE_MIN_IDLE_S` (60s)
is kept and counted as a problem instead. `--prune` also exits non-zero now: the previous loop
`continue`d past `problems += 1`, so a run that deleted gigabytes reported `0 problem(s)` and
exited 0.

# The test had to be seen red

A race test that passes on the unfixed code proves nothing, and the first version of this one did:
the window was opened on the first `config.index_path` call, which turned out to belong to
`store.connect` in the row-driven half, so it fired before the snapshot rather than between the
snapshot and the glob. Stubbing `store.connect` out puts the hook where the test means it, and it
then fails on the old code and passes on the new.
