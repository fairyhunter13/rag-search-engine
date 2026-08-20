---
type: Defect
resource: src/coderag/cli.py, tests/test_server_tools.py
title: doctor walked rows to stores and never stores to rows
description: "Every plan in the chain verified with `coderag doctor reports no orphans`. It could not see 144 index directories, 436 MiB, whose rows were gone -- because a row-driven walk starts from a row, and these had none."
tags: [cli, registry, disk, resolved]
status: resolved
generated: { by: claude/opus-5, at: 2026-08-20T20:20:00Z }
---

# What was invisible

`_doctor` iterated `registry.enabled_projects()` and asked each row about its store. Nothing asked
the other question. On this machine: 292 store directories, 148 claimed, **144 unclaimed at
436 MiB** -- mostly deleted git worktrees, `billing-core-<hash>` a dozen times over.

# The half that is easy to get wrong

The reverse walk compares against **every** row, not the enabled ones. Unflagging keeps the store by
policy, so a disabled row's directory is claimed; walking enabled rows only would have reported all
88 disabled projects as orphans and made the check useless on its first run.

The guard test creates a store with no row *and* a disabled row with a store, and asserts `doctor`
names the first and not the second. Dropping the reverse walk fails it; reporting disabled rows
fails it too.

# Where they come from, which the first count got wrong

Deleted worktrees explain some. The generator is `federation.unregister`: a member nothing else
claims leaves the registry **entirely**, and nothing deletes its store — so every live run that
registers a tmp project and releases it adds one. Six appeared during this run's own live and
restart lanes, which is how the mechanism was found at all.

So `doctor --prune` exists rather than a runbook of `rm`: deleting a store no row names is safe by
construction — nothing can reach it, and a path registered again is indexed from nothing anyway.
Unflagging is untouched, because unflagging keeps the row and the row keeps the store.
