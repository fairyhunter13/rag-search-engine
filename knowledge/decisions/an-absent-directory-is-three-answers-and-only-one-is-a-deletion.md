---
type: Decision
resource: src/coderag/prune.py, src/coderag/entry.py, src/coderag/doctor.py, src/coderag/registry.py, src/coderag/server.py
title: An absent directory is three answers, and only one is a deletion
description: A row whose root is missing is a deletion, an unmounted volume, or unreadable -- and the reconciliation reports which. Only `deleted` is actionable, and only behind an explicit flag. The hourly sweep backfills the recorded device for a present, occupied path, and refuses an empty directory because that is the shape of a bare mount point.
tags: [registry, pruning, mounts, fleet]
status: stable
generated: { by: claude/opus-5, at: 2026-09-01T00:00:00Z }
---

# The case no event reaches

[A row leaves on a delete event and never on a scan](a-row-leaves-on-a-delete-event-and-never-on-a-scan.md)
holds, and it leaves one hole: inotify has no replay. A repository deleted while the daemon was down
produces no event, ever. The row and its store stay until a person notices.

The obvious repair — walk the registry and drop the rows whose path is missing — is exactly the
predicate that wiped 236 rows. `registry.load()` refuses it for a reason, and this does not lift the
refusal.

# Three answers, not two

`prune.verdict(entry)` reads the row and returns one of:

- **`present`** — the path is there. Nothing to say.
- **`deleted`** — the path is gone, it has a nearest existing ancestor, and that ancestor's `st_dev`
  equals the `dev` recorded on the row when it was enrolled.
- **`unmounted`** — no ancestor of the path exists at all, or the one that does answers with a
  different `st_dev`. A different filesystem is serving the path today; the repository may be
  entirely intact.
- **`unknown`** — no `dev` was ever recorded, or the ancestor could not be stat'd. A reconciliation
  that cannot distinguish the first two answers must give the third.

`ProjectEntry.dev` is what makes the second and third separable, and `looks_deleted` alone is not:
a mount point left standing after its volume goes away is an empty directory whose parent exists,
which is the exact shape a deleted repository has. Without the recorded device the check degenerates
to "the path is missing", which is the wiping predicate wearing a mount test.

The hazard is not theoretical on the host this was written for: every indexed root shares one
`st_dev`, and two live `fuse.rclone` mounts sit in the same home directory with nothing indexed under
them yet. The guard is written before one is, because after is too late.

# Report first, act only when told

`coderag doctor` prints `MISSING <key> (<verdict>)` for every row whose root is gone, and removes
nothing. `--prune` acts, and only on the rows the verdict called `deleted` — an `unmounted` or an
`unknown` row is printed on both runs and removed on neither. That keeps the repo's own convention:
a read-only default, a destructive opt-in on the same command, and the destructive half never on a
timer -- `coderag-doctor.timer` is installed without `--prune` for exactly this reason.

What it removes, it removes through `registry.forget` with a key list, and the store goes into the
week-long quarantine — see
[a dead row takes its store, and the delete is a move](a-dead-row-takes-its-store-and-the-delete-is-a-move.md). So the worst case of a wrong `deleted`
verdict is a week to notice, not a re-index.

`prune.survey()` is the same verdict over the whole registry, for callers that want the census
rather than the action. It states the population it read: a filter that selected nothing is not the
same fact as a fleet with nothing wrong.

# The blind population, and why a backfill is not the scan

`dev` is written by `claim`, so a row enrolled before the field existed never had one and answers
`unknown` forever. That was **136 of 503 rows** on the first fleet this ran against: each one
unprunable by construction, and each one a page an hour away from a `rm -rf` it could not act on.

`registry.record_devices()` fills the field, and `server._sweep` calls it hourly. It reads first
and takes the exclusive lock only when there is something to write, so a complete fleet rotates no
backup.

It fills a row only when the path is a directory **with something in it**. That single test is
what keeps this a backfill rather than the wiping predicate arriving by another door: an unmounted
volume leaves its mount point standing as an empty directory, and to record the underlay's device
there would have a later verdict call an intact repository `deleted`. An empty directory is left
blind on purpose.

So `unknown` does not go away, and it is not meant to. A row already missing when the backfill runs
has taken its device with it. `doctor` prints `coderag forget <keys>` for every missing row it
cannot judge, because a verdict nothing acts on is a dead end unless the way out is written beside
it.
