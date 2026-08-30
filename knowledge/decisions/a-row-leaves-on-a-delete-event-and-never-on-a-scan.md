---
type: Decision
resource: src/coderag/prune.py, src/coderag/watch.py, src/coderag/registry.py, src/coderag/federation.py, tests/test_prune.py
title: A row leaves on a delete event and never on a scan
description: The registry refused to prune a missing path, because an unmount and a deletion look the same to a scan. They do not look the same to a delete event, so removal became automatic on the event, behind a parent test and a grace period.
tags: [registry, watcher, fleet, federation, pruning]
status: stable
generated: { by: claude/opus-5, at: 2026-08-30T00:00:00Z }
---

# The rule this replaces, and what it was right about

`registry.py` refused to prune any row for a missing path, and
[a dead row that paged hourly](../defects/a-dead-row-paged-hourly-and-nothing-could-remove-it.md)
records what that cost. The argument was that an unmounted volume, a repository moved for ten
seconds and a member behind a broken symlink all look identical to a deleted project. It was bought
with an incident: a prune by predicate wiped 236 rows when a caller ran it in-process against the
real state.

Every word of that holds — **of a scan**. A scan sees a state and has to guess how it was reached.

# What a delete event knows that a scan does not

The trigger here is one `deleted` notification on the path itself, and nothing else starts a
removal. Three tests then run, and a row dies only when all three agree.

1. The event fired on that path. No sweep of the disk begins a removal.
2. The parent directory still exists. A repository removed leaves its parent standing. An unmounted
   volume takes the parent with it. That one test separates the two cases the old rule called
   identical.
3. A grace period passes and the path is still gone at the end of it. A `git clone` into a
   moved-aside path, and any remove-then-restore, settles well inside 30 seconds.

# The link case is weaker on purpose

A member reaches this workspace through a symlink, and the link is usually what is removed while the
target lives on. No event ever fires on the target, so the link's own deletion is the only signal.

It triggers `federation.release_gone` for that root, and every member the walk no longer finds has
that root's claim released. `release` drops **one** claim and deletes the row only when nothing else
claims it. That is far weaker than the prune that wiped the fleet, which dropped rows by predicate.
A member two roots reach keeps its row. A member enrolled directly keeps its row.

So the report distinguishes them: `unclaimed` is the claim dropped, and `forgotten` is the row gone.

# What still belongs to `doctor --prune`

`doctor --prune` stays, and it is still a human's call. It reaches the rows this cannot: a project
deleted while the daemon was down, and the store directories no row claims. inotify has no replay,
so a deletion nobody was awake for is never seen here.

# Why both engines carry it

The structural engine holds the same rule in its own `prune.py`. The two must reach the same member
set, because a semantic hit's path is the root the next structural call names. A removal rule in one
engine only makes the sets diverge the first time a repository is removed.
