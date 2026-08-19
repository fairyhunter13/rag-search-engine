# A re-derive writes through the graph, it does not empty it first

**2026-07-31** · P3 · guard: `test_graph_health.py` GH5, GH8

A full graph re-derive used to begin with `GraphStore.clear()`. That is the obvious way to make a
rebuild authoritative — wipe, then re-extract, and whatever the new pass does not produce is gone —
and it is wrong for one reason that has nothing to do with correctness of the result: **`clear()`
commits.**

The empty table is therefore *published*. Every other connection to that store — the MCP server
answering `graph(relation="impact")`, `overview(what="communities")`, the coverage metrics — reads
zero symbols, and reads it successfully. There is no error, no lock contention, no partial state to
detect. The rebuild is honest about what is in the database and the database is momentarily a lie
about the repository.

"Momentarily" is the whole problem: the window is not the width of a transaction, it is the width of
a **tree-sitter walk over the entire project**, which on a large store is minutes.

## Measured

Two connections to one store, the second reading while the first ran the old sequence:

```
before clear(), other connection sees: 50
after  clear(), other connection sees: 0
```

Zero, with no extraction having run yet.

That is not a hypothetical. CI run `30619058883` failed one test out of 856:
`test_pipeline_all_stages_rse_repo`, on `stage 2 tree-sitter: 0 symbols`, against this repo's own
graph — a store that held 1,708 symbols before the run and 1,708 after. The test passed in isolation
in 0.51 s. It failed in the suite because something else in the suite triggered a full re-derive of
that project and the assertion landed inside the window.

The trigger is a **second, separate defect**, addressed below. Fixing it alone would have made this
particular failure go away without making the graph any safer to read during a rebuild — a
re-derive is a normal background event and any client can be reading during one — so the window is
closed first and on its own terms.

## What replaced it

Mark-and-sweep, in `_extract_graph`: the pass records `sid`s, walked files and edges as it goes,
writes them into the **live** tables, and only afterwards deletes what it did not just re-record
(`prune_symbols_to`, `prune_edges_to`). `upsert_symbol` and `upsert_edge` are keyed and idempotent,
so a reader sees `old | new` throughout — a superset, never a hole — collapsing to exactly `new` at
the prune. The count never dips below where it started.

Three properties of the shape are deliberate:

- **The prune subtracts what was *walked*, not what was *found*.** A file that legitimately yields no
  symbols still gets a `file_extraction` row; dropping it would shrink the coverage denominator to
  the files that happened to succeed, which turns a coverage metric into a success-rate metric.
- **Symbols are pruned before the resolution tables are built, not after.** Those tables are read
  straight out of `symbols`; a stale row surviving that far would resolve calls onto a definition
  this pass did not find.
- **`prune_edges_to` is not redundant with `purge_dangling_edges`.** The latter only catches edges
  whose endpoints are gone. The edge a re-derive has to retract is the one whose endpoints both
  still exist at the same lines — same `sid` — because the *call between them* was deleted from the
  source. `clear()` caught those by wiping the table. Nothing else does.

The incremental path (`targets is not None`) prunes nothing: it walks a subset, so absence from that
subset is not evidence of anything. All three `if targets is None` guards are that one fact.

## Rejected

- **One transaction around the whole extraction.** Correct, and it holds the WAL *writer* lock for
  the entire walk. `_update_graph_files` opens with `timeout=30`, so the watcher's incremental path
  would start failing with "database is locked" on exactly the large projects where the window is
  worst. Mark-and-sweep holds the write lock for two `DELETE`s instead.
- **Build a new `graph.db` and `os.replace` it.** The `-wal`/`-shm` files belong to the *path*, not
  the inode, so a stale WAL over a fresh inode is a corruption risk, and it would require closing
  reader connections this code does not own.
- **Staging tables for the whole pipeline.** `detect_communities` is written against the real table
  names, so this becomes surgery on `community.py` to fix a bug in `sweeps.py`.

## The trigger: a sweep that runs where the pause cannot reach it

Closing the window did not make the next run green. `318559d` failed on
`test_e1_rerank_reorders_search_results` — rerank produced scores and sorted them, but never
reordered top-1 against vector order on any of four queries. Also passing in isolation, in 10.6 s.

Same cause, different surface. `server/mcp.py`'s `index()` starts
`threading.Thread(target=reconcile_projects, daemon=True)` and never joins it. In production that is
correct and wanted: the MCP app is served by the daemon (`server/routes.py:91`), so the thread runs
inside the daemon, reads the daemon's `_PAUSED`, and stops at the pause lease like everything else.

Under pytest the tool is imported and called **in the test process**. `sweeps._PAUSED` is a module
global (`sweeps.py:9`), and the suite pauses the *daemon* over HTTP — so the test process's copy is
`False` and nothing in the suite ever sets it. The thread therefore walks all ~210 registered
projects at full speed, indexing and re-deriving, for the remainder of the session, structurally
exempt from the mechanism built to prevent exactly that. The registry is the receipt: a fleet
project shows `indexed_at 10:12:05`, inside the 10:06–10:14 CI window.

D1 did not cause this and did not hide it. It removed the graph-side symptom — the empty window the
walk used to open — leaving the same walk to collide on the vector side, where a store being
re-indexed under a running search has no equivalent write-through guarantee.

The fix is on the test side, because the product side is not wrong: `test_index_tool_e2e` now holds
`local_sweeps_paused(True)` across each `index()` call and **joins** the threads it spawned rather
than trusting them to lose the race. `local_sweeps_paused` is the in-process twin that already
existed for this exact process boundary (`tests/live/_sweeps.py:120`); it had simply never been
applied here. With the local pause set the reconcile returns at its own `is_paused()` guard and the
join costs nothing.

Not made session-wide: `test_reconcile_order`, `test_reconcile_midpass` and `test_reconcile_throttle`
call `reconcile_projects()` directly and need it to actually run. Default-paused would turn those
into tests of an early return.

## Guards

`GraphStore.clear()` still exists and still passes GH1 — it is kept for tests and for a deliberate
wipe. It has **no production caller**, and reintroducing one into a rebuild path is the regression.

- **GH5** — behavioural, replacing a source-inspection gate that asserted `"gs.clear()" in
  _index_project`. It asserted the mechanism; the mechanism was the bug. It now asserts the property
  that mechanism existed for: a full re-derive drops the symbols of a deleted file, the
  `file_extraction` row of a deleted file, and an edge whose call site was deleted while both
  endpoints survived unchanged.
- **GH8** — a concurrent reader polling `COUNT(*)` across a real `_rederive_graph` must never
  observe 0. One-sided by construction: it fails only on a sample that actually reads 0, so a
  re-derive too quick to sample yields fewer observations rather than a spurious failure. Falsified
  before landing — with `clear()` reinstated it reports **35 of 70 samples read 0**, i.e. half the
  rebuild served an empty graph.
