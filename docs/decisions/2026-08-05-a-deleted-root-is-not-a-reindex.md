# A deleted root is not a reindex

**2026-08-05** · P17 · mechanism: `daemon/sweeps.py` `on_change`, `tests/live/conftest.py`
`_purge_leaked_test_state`

Every live run left index stores behind that nothing could ever reach again: five per run, named
after the sample workspace's five projects, each holding an empty `graph.db` and `vectors.db`. They
were unreachable rather than merely unused — the registry row went with the deleted tree, and a
store dir is named `<slug>-<sha16 of the path>`, so the only handle on the dir is a path that no
longer exists. `clean-orphans` could name them; nothing else could.

Two separate causes, and each one hid behind the other.

## Cause 1 — a restored row spares the dir the sweep exists to take

The session teardown's last backstop is a listing diff: dirs that appeared during the run and that
no registry row owns. Both conditions are load-bearing (dropping either one deletes real fleet
stores, which `clean-orphans` has done once on paper). But the daemon puts the suite's rows *back* —
federation discovery re-upserts every member it finds under a registered root — and it does so from
another process. So the teardown's `purge_rows_under` only wins the races it happens to be ahead of,
and a row restored in the gap between the purge and the listing made the diff spare the dirs.

The measured signature is what makes this unambiguous: the diff printed **nothing taken** on two
consecutive full runs that each leaked five dirs.

The fix is not a faster purge or a retry loop. A row under the suite's own base can never be a
legitimate owner — `test_no_real_project_in_tests.py` is the invariant that nothing real lives
there — so `purge_unowned_index_dirs_created_since` now takes `never_owners_under` and drops those
rows from the owned set outright. The race is retired rather than narrowed. CO4 asserts both halves
as a pair: a registered dir elsewhere is still spared, and a row under a disowned base protects
nothing.

## Cause 2 — the daemon answered a deletion by writing a store

That fix took three of the five. The remaining two were written **0–3 s after teardown had already
swept**, by the daemon, and the journal says why:

```
03:33:03  watchfiles: 37 changes detected          <- the workspace rmtree
03:33:03  fts5 backfill ledger-standalone-…        <- opening a store creates it
03:33:04  fts5 backfill cart-svc-…
03:33:06  fts5 backfill promo-svc-…                <- after the sweep had run and found nothing
```

Deleting a project produces a change batch like any other, and it necessarily arrives after the tree
is gone. `on_change` indexed it anyway; indexing opens a store, and opening one creates it. So the
daemon's answer to "this project was deleted" was to write a brand-new empty store for it.

This is not a test-only defect. Any user who deletes a watched project gets the same dir, and only
`clean-orphans` will ever mention it.

`on_change` now returns immediately when the root is not a directory. A missed event costs nothing:
a root that comes back is a change of its own. Guarded by WT0 in `test_idle_stability.py`, which
fails with a store dir present when the guard is removed.

## What this cost, and the general shape

Four earlier attempts aimed at the sweep — an earlier tree walk, a row-driven purge, a listing
diff, and a graph-lane join in the teardown — and each one moved the leak later rather than ending
it. That is the shape to recognise: **a cleanup pass racing another process's writes can only ever
win the races it is ahead of.** The durable fix is to stop the write.

Verified by a full run leaving zero orphans with the teardown backstop reporting nothing to remove —
the backstop quiet for the right reason, which is the only reading of it that was ever worth having.
The two runs immediately before it were quiet for the wrong one.
