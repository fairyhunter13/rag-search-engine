---
type: Defect
resource: src/coderag/search.py, tests/test_scope.py
title: An unflagged project stayed fully searchable, because the gate asked whether the row existed
description: "`registry.get` returns disabled rows and unflagging deliberately deletes no store, so a project the user explicitly turned off answered searches by name. The error string had been claiming three conditions the code never checked."
tags: [registry, search, scoping]
status: fixed
generated: { by: claude/opus-5, at: 2026-08-20T18:00:00Z }
---

# The gate and the message disagreed

`search.py` read `if registry.get(root) is None:` and raised `"{root} is not indexed -- call
index(...) first"`. Three things are wrong with that pairing and only one of them is cosmetic:

- **`get` returns disabled rows.** `enabled_projects` filters; `get` does not. `federation.members_of`
  filters on `e.enabled` too — so a *member* of a disabled row was excluded and the **root itself
  never was**. Unflagging is the one operation guaranteed to leave the store on disk (both fleet-wide
  index wipes in this engine's history came from deleting store directories on a computed set, so
  nothing deletes one any more). Registry row present, store present, gate satisfied: an explicitly
  unflagged project was searchable by name, with no guessing required.
- **Registered is not indexed.** Claiming a root writes the row immediately and the pass that sets
  `indexed_at` runs in the background. Census at the time, counts only: **247 rows, 159 enabled, 12
  with `indexed_at`.** So 147 projects satisfied a gate whose message said they did not.
- The message was already correct. Fixing the check is what made it true.

# The predicate, and the three that were rejected

`entry is not None and entry.enabled and entry.indexed_at is not None`.

`indexed_at`, `chunk_count > 0`, `file_count > 0` and `config_signature` are coextensive — one
`registry.update` in `index.py` writes all four — so any of them would do, except that
**`chunk_count > 0` fails a legitimately empty project**. Store-stamp compatibility is a different
question: it is the rebuild trigger. Store-directory presence is not a valid predicate at all: of
**78 directories, 13 matched a registry row and 65 matched none** — 212 MiB with no project behind
it, left by releases and renames, since nothing here deletes a store. Presence answers a question
about this engine's own history, not about whether a project is indexed.

# Why nothing caught it

There was no test that registered a project, disabled it, and then searched it. Every existing test
either searched something it had just indexed or searched something never registered, and both of
those pass under the old predicate. The two that cover it now are built from `tmp_path` fixtures in
the autouse-isolated registry and assert shape only, never a real path — see
[the-search-unit-is-the-callers-own-workspace](../constraints/the-search-unit-is-the-callers-own-workspace.md), which is the constraint this defect sits under.

Falsified by reverting the predicate to `entry is None`: seven tests in `tests/test_scope.py` fail,
including both of these. One of them then loads a model on the way down and the interpreter aborts
at finalization — which is [a-cancelled-task-group-cannot-reach-a-shielded-thread](a-cancelled-task-group-cannot-reach-a-shielded-thread.md)'s CUDA-134
exit showing up as a side effect, and incidental confirmation that a removed gate really does fall
through into `search.search`.

# What it means operationally

Tightening this takes the searchable set from 159 to 12 the moment it lands, so the fleet index
ships in the same unit of work. The refusal names `index(root=...)`, which is a fix a model that
lands on one of the other 147 can apply itself in one call — asserted, because a refusal that does
not name the next call is a dead turn.

The served `INSTRUCTIONS` had to move with it. They said *"Never index a project the user did not
ask you to index"*, which — once the gate tightened — forbade the one call the refusal tells the
model to make, on the model's own workspace. They now name `index` as the fix for that refusal and
keep the guard where it was actually earned: any *other* project, ask first. The pin makes "any
other project" a much smaller set than it was.
