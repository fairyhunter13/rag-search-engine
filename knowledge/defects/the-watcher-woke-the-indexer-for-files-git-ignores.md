---
type: Defect
resource: src/coderag/watch.py, src/coderag/discover.py
title: The watcher woke the indexer for files git ignores, forever
description: "`discover.indexable` is the shared predicate, but it never sees gitignore -- the indexer gets that from `git ls-files --exclude-standard`. So a gitignored build cache, which no pass will ever index, submitted a full-project job on every write to it. Caught mid-fleet-index: `3 changes detected` every ~4 s and a queue that grew while the worker drained it."
tags: [watcher, indexer, inotify]
status: stable
generated: { by: claude/opus-5, at: 2026-08-20T18:20:00Z }
---

# The symptom

During the fleet index the queue grew from 130 to 157 while `completed` went 50 → 84. The journal
showed a steady `3 changes detected` every four to five seconds with no user activity.

The source was one repo's `.turbo/cache/` — a build tool rewriting manifest JSON on a loop. The
directory is in that repo's `.gitignore`, so `candidates()` never returns a file from it. Every
job it caused diffed the whole project and wrote nothing.

# Why the shared predicate did not catch it

`discover.indexable` is deliberately shared between watcher and indexer so the two cannot drift.
It covers `DEFAULT_IGNORES`, the config's own excludes, secrets, images and binary extensions —
every filter that needs no disk read. Gitignore is not one of those: the indexer gets it for free
from `git ls-files --cached --others --exclude-standard`, which the watcher never calls. The drift
the docstring rules out existed one layer above it.

# The fix

`discover.git_ignored(project, rels)` runs one `git check-ignore --stdin -z` per project per
batch, with the same `--exclude-standard` semantics. `_dispatch` subtracts it before submitting,
only when the project's config respects gitignore.

Guard: `tests/test_watch.py::test_a_gitignored_build_cache_never_reaches_the_queue`, verified to
fail with the subtraction removed. It asserts the tracked write in the same batch still submits,
so a filter that drops everything cannot satisfy it.
