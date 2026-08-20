---
type: Defect
resource: src/coderag/cli.py, tests/test_server_tools.py
title: doctor walked rows to stores and never stores to rows
description: "Every plan in the chain verified with `coderag doctor reports no orphans`. It could not see 144 index directories, 436 MiB, whose rows were gone -- because a row-driven walk starts from a row, and these had none."
tags: [cli, registry, disk, resolved]
status: stable
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

# Amendment, 2026-08-20: two claims corrected and the backlog closed

**"Unflagging keeps the row" is wrong**, and the safety argument above leaned on it. `registry`
deletes a row that no root claims any more, so unflagging a federated member removes the row and
leaves the store — which is exactly how an unclaimed store is generated. The remaining half stands
and is what `--prune` actually rests on: no row names the store, and a path registered again is
indexed from nothing. The same sentence was in `cli.py` as a comment and is gone.

**The six stores were not attributed correctly.** `tests/test_restart.py` runs on its own
`CODERAG_STATE_DIR`, so its stores are never counted here; the live lane is the generator.

The 143-store, 0.46 GiB backlog is pruned. As of this measurement the registry holds 236 rows and
**0** unclaimed stores. And the walk itself moved: it lives in `registry.unclaimed_stores()` now,
called by both `doctor` and `/healthz`, because the fleet fixture needs the same number. What that
walk got wrong under concurrency is [prune raced a store the daemon was
writing](prune-raced-a-store-the-daemon-was-writing.md).
