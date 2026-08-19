---
type: Runbook
resource: src/coderag/registry.py, src/coderag/config.py
title: Restoring the registry, the one file that cannot be re-derived
description: projects.json holds which projects are indexed, which roots claim them and which are enabled; the indexes can be rebuilt from disk and it cannot, so it is backed up on every write.
tags: [registry, recovery, operations]
status: stable
generated: { by: claude/opus-5, at: 2026-08-19T10:30:00Z }
---

# Why this file gets its own runbook

Every other piece of state here is derivable. An index is a function of the files on disk; a
vector is a function of a chunk. `~/.local/share/coderag/projects.json` is the only thing that
records a **decision** — which projects someone chose to index, which roots claim which members,
and what is enabled. Nothing on disk implies it, and it has been destroyed twice.

# Restoring

`registry._save` rotates a copy into `~/.local/share/coderag/backups/projects.<stamp>.json` **before
every write**, keeping the newest 20. So:

1. Stop the daemon. A running writer will rotate your damaged file into the backup set and then
   overwrite whatever you restore.
2. `ls -t ~/.local/share/coderag/backups/` and pick the newest backup from **before** the damage —
   check the row count, not the timestamp, because the damaging write is itself recent.
3. Copy it over `projects.json`. Do not merge by hand: the file is small, and a hand-merged registry
   with a duplicated path is a project that indexes twice.
4. Start the daemon. The startup reconcile is a content-hash diff, so a restored row whose index is
   stale converges on its own. **No re-index is needed for a registry restore.**

# The traps that produced the two losses

**Do not read the registry with anything that prunes.** The wipe that cost the fleet its rows came
from a listing path that dropped rows whose path was missing on disk — a detached USB drive or an
unmounted share is enough. A read that writes is not a read.

**Do not restore from a snapshot taken by a process that also writes.** `_mutate` flocks the
sidecar and **loads inside the lock**; that is the fix for a measured lost update where two writers
each read 180 rows, each added one, and the survivor kept **34**. A restore script that reads
outside the lock reproduces it exactly.
