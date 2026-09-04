---
type: Constraint
resource: src/coderag/doctor.py, src/coderag/prune.py, src/coderag/cli.py, src/coderag/quarantine.py
title: Three of the four store removals are reversible, and the unclaimed one is not
description: "`doctor --prune` runs both removal paths 29 lines apart: a MISSING row's store is moved into .trash, and an unclaimed store is rmtree'd outright. The discriminator is whether a row ever named the store, not whether a human typed the command. Measured 2026-09-04: 69 unclaimed stores deleted with no undo, 207 MiB, while 10 stores of the same fixture names sat recoverable in .trash from the reaper that morning."
tags: [pruning, storage, registry, quarantine, undo]
status: stable
generated: { by: claude/opus-5, at: 2026-09-04T15:30:00Z }
sources:
  - id: doctor-prune
    resource: src/coderag/doctor.py
  - id: quarantine
    resource: src/coderag/quarantine.py
---

# The split is the row, not the caller

[A dead row takes its store, and the delete is a move](../decisions/a-dead-row-takes-its-store-and-the-delete-is-a-move.md)
is scoped to the reaper, and reads as a property of the system. It is not. Four paths remove a
store and only three of them are undoable:

| path | what named the store | removal |
|---|---|---|
| `prune.py:81`, the reaper | a row a delete event dropped | `quarantine.take` |
| `doctor.py:53`, `--prune` on a MISSING row | a row whose verdict is `deleted` | `quarantine.take` |
| `cli.py:97`, `coderag forget` | a row a human named | `quarantine.take` |
| **`doctor.py:82`, `--prune` on an unclaimed store** | **nothing — no row exists** | **`shutil.rmtree`** |

The first three are `.trash/<unix-ts>-<name>` and `QUARANTINE_DAYS` of undo. The fourth is gone at
the moment it prints `pruned`.

**The two live in the same function.** `doctor.run` quarantines on line 53 and `rmtree`s on line
82, in one invocation of one flag. So "was this reversible?" cannot be answered from the command,
from the caller, or from whether a person typed it. It is answered by whether a row ever named the
directory.

# Why the reasoning behind quarantine does not reach the unclaimed half

Quarantine exists because both fleet wipes surfaced days late, the same way: someone searched a
repository and got nothing back. Undo needs somewhere to put the bytes *back to*, and an unclaimed
store has no row to restore them to — the directory would return and the registry would still not
name it.

That is a real asymmetry and it is not the whole argument. An unclaimed store is also what a
project looks like after its **row** is lost, which is the case
[restoring the registry](../runbooks/restoring-the-registry.md) exists for. So the removal with no undo is the one
covering the state a registry loss produces. Nothing here is wrong; the reader who assumes the
card above covers `--prune` is.

# Measured, 2026-09-04

`doctor --prune` reported `0 problem(s), pruned 69 store(s), 207 MiB`, taking the index directory
from 485 to 416. Every one was pytest residue: `root`, `member`, `stranger`, `storefront`,
`billing-core`, `wroot0`, `wmember0`. None reached `.trash`.

`.trash` held 10 stores at 32 MiB, moved there by the reaper at 05:04 the same morning — and
**four of those carry the same fixture names**, `wmember0` and `wroot0`, plus `storefront` and
`billing-core`. Same names, same day, same directory: one set recoverable for seven days, the other
unrecoverable on the spot. The name tells you nothing about which.

`--force` was not used. 69 of 485 is 14%, under the half-the-tree refusal in `disk.refuse_on_shape`.

# What would change this

Routing the unclaimed branch through `quarantine.take` costs one line and makes the whole command
reversible. It is not proposed here, because the undo would restore bytes no row claims, and the
orphan walk that named them would name them again on the next pass. The case for it is a registry
loss, and the runbook for that is the place to argue it.
