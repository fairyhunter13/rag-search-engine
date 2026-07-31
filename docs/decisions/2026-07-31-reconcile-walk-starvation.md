# The re-derive that never reached 89% of the fleet

**2026-07-31** · guards: RO4-RO6, RC1-RC4 in `test_reconcile_order.py`; AU5 in
`test_extraction_ladder.py` · code: `reconcile_order`, `_resume_at`, `reconcile_projects`
(`daemon/sweeps.py`), `_pipeline_block` (`server/_overview.py`)

The content-addressed store re-derives a graph when its stored `meta.algo_version` differs from
`_pipeline_algo_version()`. The mechanism was correct and had been for months. Measured across the
144 enabled stores holding a `graph.db`: **128 were stale**, 51 of them four extractor revisions
behind — behind the revision that *added* the `language_mismatch` rung, so ~2,400 files whose bytes
are XML under a source extension were still stored as `generic` at a measured error-byte ratio of
0.998, far past the 0.5 threshold that would have caught them. The rung logic was right and had
simply never run on them.

**Throughput was not the constraint, and that was the first wrong answer.** Complete passes over
~200 projects measure 46.5 / 81.8 / 100.1 / 106.4 s. The one-core daemon quota is not the problem
and should not be raised.

The walk was. `reconcile_projects` returns on `is_paused()` and kept no cursor, so the next pass
restarted at position 0 — nine consecutive passes abandoned at 1, 0, 9, 52, 0, 2, 1, 2 and 16 of
~216. Meanwhile `reconcile_order` keyed on `(not has_vectors, last_change_seen)`, a key added for a
*different* drift species. For a graph re-derive every store already has vectors, so that first key
is constant across the whole population and the sort degenerates to recency — under which a stale
store, being by definition one nothing has touched lately, sorts to the **tail** of a `reverse=True`
walk. Current stores sat at positions 59-74; stale ones spanned 58-201, median 137. **Zero of the
128 were reachable by any pass that had actually run.**

A live suite holds the sweeps pause lease for its entire run, so truncation is the normal case,
not the exception: running the tests was the thing preventing the rebuild the tests were waiting on.
The docstring had named this in prose and it stayed unfixed, because nothing counted it.

**Three fixes, one theme.** A persisted resume cursor beside the registry (rotation, not an integer
offset — the walk is re-sorted and re-sized every pass, so an index points at an arbitrary different
project next time). A pipeline-drift sort key between the existing two: never-embedded still wins,
because no vectors returns *nothing* for a search while drifted returns real results from an older
extractor. And a `pipeline_version` block in `overview(what="metrics")`, riding the store
connections the extraction block already holds.

**Verified on the live daemon, not just in tests.** An unpaused walk was truncated mid-flight by a
normal `POST /api/sweeps/pause`; it logged `abandoned at 206/210 (sweeps paused), resuming there
next pass` and wrote the project at position 206 to the cursor file. The daemon was then restarted,
and the first project the fresh pass touched was that same one — the cursor is a file rather than a
module global precisely so a restart cannot lose it, and this is the case that shows why. Without
it the pass would have begun at position 0, which is the whole defect.

Two things that measurement also settled. The cursor is written only on a *graceful* abandon or a
completed lap, so a hard restart mid-project falls back to the last graceful position — still
strictly better than 0, but not a transaction. And **a pause takes effect at the next project
boundary, not immediately**: `is_paused()` is checked once per project, and one project's graph
re-derive was measured at over nine minutes with the daemon at 94% of its one-core quota. A suite
that pauses sweeps does not get a quiet daemon straight away; it gets one when the project in
flight finishes. That check location predates this work and is unchanged by it, but anything timing
a measurement against the pause needs to know it.

**The transferable rule:** a self-healing mechanism needs a reported convergence number, not just a
correct repair path. Every component here worked in isolation — the staleness comparison, the
re-derive, the pause. What nobody could see was whether the loop closed, and a fleet that had
stopped converging was indistinguishable from one that had finished. The instrument came last and
should have come first; it is what turned "the extractor seems behind on some repos" into a number
that has to fall pass over pass.

**The secondary rule:** a priority key written for one drift species silently mis-sorts the next
one. The key was correct when added and became wrong when a second kind of staleness appeared
underneath it, with no failure anywhere — just a population that never got walked.

Related: [reload under a sweeps lease](2026-07-29-reload-under-a-sweeps-lease.md) — the same lease,
the other way round; [one live suite at a time](2026-07-30-one-live-suite-at-a-time.md) — why the
lease is held so much of the time in the first place.
