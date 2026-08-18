---
type: Defect
resource: src/rag_search/core/config.py
title: The incremental path stamped no freshness
description: A watched, fully current project read as stale, because the only freshness field was written by full re-index runs and the incremental watcher path touched the registry not at all.
tags: [registry, observability, watcher, defect]
status: resolved
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# The incremental path stamped no freshness

## Symptom

A project under active watch, with every change indexed within seconds, reported as stale. The
data was right; the number describing it was wrong.

## Root cause

`indexed_at` was written only by `_index_project` — the full-index path. Steady state does not run
full index; it runs `_index_files` off a watcher event (see
[the graph lane wakes only on code drift](../constraints/the-graph-lane-wakes-only-on-code-drift.md)),
and that path did not touch the registry at all. So the field was measuring "when did this project
last take the slow path", under a name that reads as "how current is this".

## Why nothing caught it

Observability-only: no query returned a wrong result, so nothing downstream broke. And no test
asserted that the *watcher* path advanced a freshness field — the field had tests, but they all
went through a full index.

A number nobody's assertion depends on is a number that can be wrong indefinitely.

## What covers it now

`ProjectEntry.last_change_seen` is a separate field with an honest name, stamped on the incremental
path in `daemon/sweeps.py` and surfaced through `server/_overview.py`. It is also the ordering key
reconcile uses, which gives it a second consumer that fails loudly if it stops advancing — the
cheapest defence against a field going inert again.

Guards: `test_p20_indexed_at_stamped` asserts `last_change_seen is not None`,
`test_p22_incremental_reindex_idempotent` covers the rerun, and
`test_wk1_a_write_is_retrievable_from_that_projects_own_store` in `test_watcher_registry_sync.py`
holds the watcher path end to end.

The general rule this fed into is SC11 — every config field must have a reader outside its own
loader and outside the reporting surface. See
[one predicate decides what is indexed](../constraints/one-predicate-decides-what-is-indexed.md).
