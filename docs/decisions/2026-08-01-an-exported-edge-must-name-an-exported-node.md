# An exported edge must name an exported node, and a paired result must stay paired

**2026-08-01** · P5 · `server/routes_graph.py`, `scripts/eval_retrieval.py`

Two items had been carried as cheap follow-ups for several plans — *"add an `ORDER BY`"* on the graph
export, and *"fix the eval harness"*. The first was mis-sized: it is not a determinism nicety, it is
a correctness bug that made the export's edge list **entirely useless** on the shape of project the
dashboard is most often pointed at. Both are fixed here. No stamp, no re-derive, no GPU hours.

## The graph export returned edges to nodes it never sent

The route selected its two halves with independent `LIMIT`s:

```sql
SELECT sid AS id, name, kind FROM symbols LIMIT ?
SELECT caller_sid AS source_id, callee_sid AS target_id FROM edges LIMIT ?
```

Nothing constrains an exported edge's endpoints to be in the exported node set, and at scale
essentially none of them are. Measured across all 155 stores at the `max_nodes=2000` the dashboard
sends:

| scope | exported edges | usable | dangling |
|---|---:|---:|---:|
| fleet, 155 stores | 70,864 | 34,546 | **51.3%** |
| the largest store (120,249 symbols) | 2,000 | 0 | **100.0%** |
| a 136-member federation | 2,000 | 0 | **100.0%** |

The federated case is the one that matters, and it is total. The route collected 133,087 nodes and
59,492 edges across the members, then truncated each list to 2,000 **separately** — so the surviving
nodes came from the first member and the surviving edges from wherever the concatenation happened to
reach. Not one exported edge joined two exported nodes. The graph view had been drawing 2,000
isolated dots and 2,000 edges to nothing.

**An `ORDER BY` would not have fixed it.** It makes an arbitrary subset reproducible; it does not
make two independently-chosen subsets agree. The fix is that the edge set must be *induced* by the
node set, which means choosing the edges first and letting the nodes follow.

### Edge-first is also the better use of the budget

Three strategies, measured fleet-wide at `max_nodes=2000`:

| strategy | nodes | edges | usable | dangling |
|---|---:|---:|---:|---:|
| independent `LIMIT`s (before) | 147,742 | 70,864 | 34,546 | 51.3% |
| induced on sid-ordered nodes | 147,742 | 41,803 | 41,803 | 0.0% |
| **edge-first, then induced (now)** | **61,304** | **88,299** | **88,299** | **0.0%** |

Edge-first returns **2.6× more usable edges than the old scheme exported in total**, from 41% of the
node slots, because the budget goes to the connected part of the graph rather than to an arbitrary
prefix of `symbols`. Leftover budget is then filled with unconnected symbols, so the "up to
`max_nodes` symbols" contract still holds for the 15 stores that have no edges at all.

The budget is now global rather than per member — `federated_map` runs the export once per store in
order and the closure carries what is left, which is what removes the second truncation. One fact
made this simpler than it looks: **there are zero orphan edges fleet-wide** (0 of 122,478 name an
endpoint missing from `symbols`), so the induced construction needs no defensive filter behind it.

### A federation member is not a disjoint store, and the first fix assumed it was

The first version concatenated members without deduplicating, on the reasoning that `sid` is a
content hash rather than a per-store sequence, so ids could not clash. The new test said otherwise
on its first run: **194 node rows for 98 symbols**. The reasoning was right about collisions and
wrong about overlap. `expand_federation` returns `[root, *members]`, and a root whose directory
*contains* its members has indexed their files too — so the same file is walked by two stores and
produces the same rows twice. Edges duplicated identically; only the node assertion happened to be
the one written first.

That the id is a hash of the **absolute** path (`file:name:start_line`) is what makes the fix safe
rather than lossy: two different symbols can never produce one sid, so an equal sid is the same
symbol seen through two stores. Deduplicating is a union, and it cannot fuse two repos' symbols into
a cross-repo edge (P3). Fleet-wide this overlap does not currently occur — 0 sids appear in more than
one of the 155 stores, across 377,559 distinct ids — which is exactly why only a fixture built to
have it could witness the bug.

### The gate that did not exist

`test_api_graph_export` asserted `status_code == 200` and `isinstance(data, dict)` — it passes against
an empty object, and it stayed green through every measurement above. The new test sits *beside* it
rather than replacing it — the weak one is the only coverage of the single-project path, and a smoke
test is worth keeping once it is no longer the only thing asserted. The new one asserts the invariant
itself over the *federation root*, at a cap tight enough to force truncation and again at one loose
enough to prove the fixture is not merely too small to cap, and it is non-vacuous by construction: an
empty edge list would satisfy "every edge is induced" for free, so a non-empty edge list is asserted
first.

## The eval harness threw away the records its own comparisons needed

Four defects, all in `scripts/eval_retrieval.py`:

1. **`nDCG@10` was never a second signal.** With one gold document and binary relevance it returned
   `1/log2(rank+1)` against RR's `1/rank` at the same rank — a deterministic transform of a column
   already in the table. The old docstring conceded the arithmetic while every table in the lineage
   presented the two as independent columns. **Retired**, not recomputed: the tables already on the
   record keep theirs.
2. **No paired test was possible.** `evaluate` accumulated straight into a 4-element total and
   divided, discarding per-query outcomes. Arms run the *same queries against the same corpus*, so
   comparing two arms as independent means throws the pairing away. Runs now carry per-query ranks
   and `--compare` applies **McNemar's exact test** to the discordant pairs. Exact binomial via
   `math.comb`, not chi-square: the discordant count here is routinely under 25, which is where the
   approximation is least trustworthy, and there is no scipy in the venv to justify adding.
3. **The reranker's ceiling was unreportable.** `recall@50` is now reported in the dense lane, with
   the depth imported from `query/search.py::_MIN_POOL` rather than hard-coded so it cannot drift
   from the pool the cross-encoder is actually given. A gold chunk outside that pool is one no
   reranker can recover. First reading on this repo: **1.0** — pool depth is not the bottleneck
   here, which retires `_MIN_POOL` as a tuning candidate on this corpus. Dense lane only: `search()`
   returns results already reranked, so a hybrid figure would mean duplicating production fusion
   inside the harness to reconstruct a pool it never sees.
4. **MRR keeps its @10 cutoff** even though the dense lane now retrieves to 50. Widening it would
   silently rebase every MRR already on the record.

The per-query records carry a 12-hex digest of the query text and a rank — never the gold path. The
query set is deterministic and model-independent, so the same query hashes identically in both arms,
which is all an alignment needs; and the records add no P18 surface the result file did not already
have.

## Transferable

- **Two independent `LIMIT`s over related tables do not sample a subgraph.** They sample two things
  that happen to have the same size. If one table references the other, one query has to be induced
  by the other.
- **A test that asserts a 200 and a type cannot witness a content bug.** Both of the assertions it
  sits beside were true for the entire life of the defect — and the new test went red on its first
  run against a bug in the fix itself, which is the only reason the overlap was found at all.
- **"Ids are hashes, so they cannot clash" answers the wrong question.** The hash rules out
  *collisions*; it says nothing about the same row being read twice through two stores. The fleet
  had no overlap to show it; a three-service fixture did.
- **Check the shape of the result before deciding what a fix costs.** This was ranked last across
  two plans as "one line" on the strength of the symptom named in a comment, not of a measurement.
- **When results are paired, pairing is free power.** The discordant-pair test resolved a difference
  at n=30 that the unpaired intervals could not have separated.
