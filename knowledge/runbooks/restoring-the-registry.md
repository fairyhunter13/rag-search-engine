---
type: Runbook
resource: src/rag_search/core/registry.py
title: Restoring the registry
description: "`projects.json` is the one file in the data dir that cannot be re-derived — where its copies come from, and why a restore goes row by row rather than by replacing the file."
tags: [registry, backup, recovery, operations]
status: active
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# Restoring the registry

Two things live under `$XDG_DATA_HOME/rag-search/`:

| Path | Env override | Re-derivable? |
|---|---|---|
| `projects.json` | `RSE_REGISTRY_PATH` | **No.** It is the list of what the fleet is. |
| `indexes/<slug>-<sha256[:16]>/` | `RSE_INDEX_ROOT` | Yes, at the cost of a full GPU re-embed. |

So the registry is the thing to back up, and it is small. The stores are not worth backing up —
they are a cache with an expensive miss.

## Where the copies come from

Nothing runs on a timer. Both copies are side effects of something else, which is why you should
know when they are and are not written.

**`projects.json.bak.1` … `.bak.5`** — a five-deep ring pushed by `_rotate_backups()` from inside
`_mutate`'s lock, and **only when the pending write drops keys**. Rotating on every write would be
useless: `register_all_members()` upserts once per discovered member at each daemon start, so one
startup would consume the ring several times over. Gated on removal, the ring holds only deletions,
and a deletion is the only thing anyone has ever wanted back. A rotation then suppresses the next
`_BACKUP_COOLDOWN_S` (600 s) of them, so a removal burst is captured once — at the state it started
from. Measured: without that cooldown, eight sequential removals left even the *oldest* copy already
missing three rows.

**`projects.json.session-snapshot`** — written by `take_snapshot()` at the start of every live
session, beside the registry rather than under the suite's scratch base, because its whole value is
outliving a run that was killed. It stays on disk afterwards as the operator's handle.

Neither is a substitute for the other: the ring tracks deletions, the snapshot tracks sessions.

## Restore row by row, never by replacing the file

Put missing rows back through `upsert_project`, which takes the same flock every other writer takes.

Do **not** `os.replace` a snapshot over `projects.json`. The daemon writes `indexed_at` continuously
— a rebuild is roughly 1,700 chunks/min of registry churn — so dropping a session-old file on top
rolls back every stamp written since, trading a loud failure for a quiet one. Same reasoning as
[the incremental path stamped no freshness](../defects/the-incremental-path-stamped-no-freshness.md):
a freshness field nobody is asserting on can be wrong indefinitely.

## The refusal you will meet on the way

`orphan_dirs()` raises `OrphanSweepRefusedError` when the registry holds zero rows while
`INDEX_ROOT` holds stores. Stores are only ever written for a registered project, so that state
means the registry was lost, not that the projects were — and the sweep says so instead of
finishing the wipe. `allow_bulk=True` lifts the majority cap for an operator who has read the
refusal; it deliberately does **not** lift the empty-registry refusal, because with zero rows there
is nothing left to check the decision against.

Restore first. Then sweep.

## Why this file exists

On 2026-07-30 a teardown helper's predicate was broken deliberately, to demonstrate a red test. It
runs from a session autouse fixture *inside the test process*, so the broken predicate matched the
whole fleet: **198 rows and 138 stores deleted from the real registry.** `os.replace` keeps no
history, ext4 has no snapshots, and nothing had ever dumped the file. There was nothing to roll back
to, and the fleet cost a full GPU re-index.

`_registry_guard.py` now closes the class rather than the one door: every live session snapshots
first, and refuses to finish quietly if a row present at start is gone at the end. Its assertion is
scoped, not absolute — `purge_rows_under(_SAFE_BASE)` and `_migrate()`'s dead-registration prune and
canonical re-key all remove rows legitimately, and a guard that flags those is a timer, muted inside
a week. A row counts as lost only when it was present at start, is absent now, sits outside the
suite's base, **still exists on disk**, and its canonical form is absent too. That combination is
the wipe signature and nothing else.

Procedure that runs alongside this one: [running the live suite](running-the-live-suite.md).
