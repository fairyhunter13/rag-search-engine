---
type: Decision
resource: src/coderag/federation.py, src/coderag/projcfg.py, src/coderag/server.py, tests/test_federation.py
title: Membership is a directory symlink at depth four or less, and the sweep is what makes that automatic
description: Both halves of the membership predicate were inherited from the deleted v1 engine and argued nowhere. The depth limit is now measured to cost zero on the live tree, and a declarative member list stays refused. Re-discovery moved off the explicit index call onto an hourly sweep.
tags: [federation, discovery, registry, watcher, scheduler]
status: stable
generated: { by: claude/opus-5, at: 2026-08-22T00:00:00Z }
---

# What a member is, and why nothing else is

A member is a **directory symlink** under a root, resolving to a project outside that root's own
tree. `federation.discover` is the only membership source in shipped code, and the symlink is the
whole predicate. There is no `include` key, no member list, no path glob.

That was never written down. Both halves arrived in the foundation commit from the deleted v1
engine. `federation.include` appears nowhere in this bundle, in `git log`, or in the code, so it
was **absent rather than refused**. `MAX_DEPTH = 4` silently narrowed the predecessor's any-depth
walk with no number attached. `_FEDERATION_KEYS = {"exclude"}` in `projcfg.py` means the per-project
file can only subtract from what the walk finds.

The symlink half is right for a reason already recorded. The link is a *discovery* mechanism and
the resolved target is the key, so the watcher covers members by construction. Inotify does not
traverse symlinks and fails silently when it tries.
A declarative member list would put a config file in every one of ~159 repos, to express what a
symlink already expresses. It is the same duplication the global-MCP answer rejects.

# The depth limit, measured

Four is now a number rather than a habit. On the one root that federates — 142 members, 92% of all
193,513 chunks in the fleet — the links sit at these depths:

| depth | 2 | 3 | 5 | 6 | 7 | 8 |
|---|---|---|---|---|---|---|
| links | 142 | 59 | 14 | 74 | 3 | 2 |

`MAX_DEPTH = 4` cuts everything below the line. Of the **93 links beyond it, zero are lost**. Each
one resolves to a target that is already a member through a shallower link, or is matched by
`federation.exclude`, or fails `_looks_like_a_project`. Raising the limit today changes the member
set by nothing and buys a deeper walk of every root.

So the limit is kept, and the kill criterion is stated rather than left to be rediscovered. **if
that count is ever non-zero, the limit is wrong and raising it is the fix** — not an `include`
key. The
probe is a walk to depth 12 applying `discover`'s own excludes and dedup, which is why it can
distinguish "beyond the limit" from "actually missing".

# Discovery had no clock, which is what made it feel wrong

`discover`/`register` were called from exactly two places, `cli.py` and `tools.py`. Both are an
explicit `index` call. So a symlink added to a root after its last `index` was **never** picked
up. `reconcile_all` had no caller at all, though the docstring claimed the tick ran it.

`federation.sweep()` re-runs `discover` over every enabled direct root, claims what is new and
returns it. `server._sweep` submits those, calls `reconcile_all`, and re-arms. It runs on a
counter inside the existing tick, `SWEEP_EVERY_S`, default hourly. Not per tick, because re-arming
tears down ~120,000 inotify watches at 5.4 s over 151 projects. So the pass has to go through
`rearm_if_changed`, and must not re-arm on an unchanged set.

**The sweep only ever adds**. A link that is gone and a link whose target is briefly unmounted are
the same observation from here. The registry's rule is that removal is explicit. The pruning
version of that rule is what wiped the fleet registry once already. One unparseable
`.coderag.yaml` records `last_error` on its root, and the sweep continues. That is also how a
project dropped from the watch set for a broken config comes back without a daemon restart.

The hourly pass re-enqueues all 149 rows, which is why `index.submit` now collapses an identical
whole-project walk already waiting. Partial jobs never dedup. A partial names files a queued walk
may already cover. Dropping it on that guess loses a write, where a redundant walk wastes one.

The queue is the state. A shadow set of pending projects has to be cleared by whoever dequeues.
Anything that is not the worker then leaves it claiming a job that no longer exists.