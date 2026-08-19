# A fleet number behind a project-scoped gate

**2026-08-05** · P3 · mechanism: `server/_overview.py` `_extraction_block` / `_pipeline_block`

`pipeline_version.stale_stores` is the convergence instrument. `CLAUDE.md` instructs an operator to
watch it fall after a stamp move, and `2026-07-31-releasing-a-lease-schedules-nothing.md` was
written because it was not on screen when it needed to be.

It could not be read fleet-wide by any sanctioned call.

```
overview(what="metrics")                 -> extraction: {"error": "project_path required …"}
                                            pipeline_version: ABSENT
overview(what="metrics", project_path=…) -> pipeline_version: {"stores": 1, …}
```

The unscoped call — the one you make to ask a fleet question — had no `pipeline_version` key at
all. The scoped call had one, reporting a single federation. Neither answers "has the fleet
converged?", which is the only question the block exists for.

## Why it was shaped that way

`_pipeline_block` was computed at the tail of `_extraction_block`, on stamps that function had
already collected while holding each store open. As an efficiency argument that was correct and it
is why the placement looked free. But `_extraction_block` is **project-scoped**, and it early-returns
when no `project_path` is given and none can be inferred. This fleet has 150 enabled projects, so
`_require_project` refuses on *every* unscoped call, and the return happened before the tail.

A fleet-scoped number was computed as a side effect of a project-scoped walk, so it inherited that
walk's scoping — including its refusals.

## Two correct changes, one day apart, that combined into silence

- **2026-07-30 — EL13.** `_extraction_block` returned a bare `{}` on an unresolvable project, which
  read as "the ladder recorded nothing". The fix returned an `error` dict instead, so "I was not
  asked" and "I found nothing" became distinguishable. Right, and still right.
- **2026-07-31 — `_pipeline_block`.** Added at the end of the same function, *below* the early
  return that EL13 had just made meaningful. Right in isolation.

Neither change is wrong. The defect exists only in their composition, which is why review of either
diff would not have surfaced it.

## Why the guard did not catch it

AU5 asserts `_pipeline_block`'s counting rules against a hand-built `{stamp: count}` dict —
`(unstamped)` counts as stale, `by_stamp` leads with the largest population, the parts sum to the
whole. All true, all still true, and none of it reachable.

**A guard on a helper cannot observe the helper being orphaned.** AU5 passed every run for five days
while the only caller that mattered could not reach the code it guards. This is the same class as
the last several entries in this directory: a check is not in force until something has watched it
pass *through the surface a caller actually uses*.

## The fix

`pipeline_version` is now a **top-level sibling of `extraction`** in the metrics payload, tallied by
`_fleet_pipeline_block` over `searchable_stores(list_projects())` — always the whole registered
fleet, never narrowed by `project_path`.

Always fleet-wide rather than fleet-when-unscoped: one key with two meanings is a number that gets
misread, and a stale count over a one-member federation was never what anyone wanted. There is no
`scope` discriminator because there is nothing to discriminate.

`_pipeline_block` is unchanged and still does the arithmetic AU5 guards; the walk that feeds it is
what moved. Cost over 150 stores, measured: **89 ms**, one indexed `meta` lookup each. A bare
`sqlite3` handle does the same walk in 24 ms — the difference is `GraphStore`'s setup, and it is the
price of reading through the writer rather than around it. Do not optimise it back to a raw handle:
these stores are WAL and the daemon is their writer, so an outside reader can be served a
pre-checkpoint snapshot and return a wrong answer that is stable and reproducible (AU1). Stores are
opened and closed one at a time, keeping the descriptor peak at one — the 2026-07-29 wedge was
descriptor exhaustion from holding a federation open at once.

`searchable_stores` moved from `scripts/purge_unindexable.py` into `daemon/federation.py` so both
callers share one definition rather than two that drift.

## The new guard, and what makes it different

**AU6** goes through `handle_overview("", "metrics")` — the surface, not the helper. It asserts the
key is present, that the counts are ints partitioning the fleet, and, as the direct statement of the
regression, that **`extraction` refused and `pipeline_version` answered anyway** on the same call.
It also asserts the key is not nested back under `extraction`, which is how a future refactor would
restore this silently.

Confirmed a real negative control: run against a payload with the key removed, AU6 fails with the
message naming the defect. Shape assertions only — no store count, no stamp string, because both
move on every re-derive and a test that pins them goes red on the event it exists to confirm.

AU5 keeps guarding the arithmetic. Both are needed; the absence of the second was this defect.

## Scope boundary worth stating

The count is registry-driven, so an index dir with no registry row is invisible to it. That is
deliberate. Orphaned store dirs are Guard 6's problem and the `safe_tmp_path` fixture's — conflating
them is what once left orphan dirs holding `stale_stores` above zero for reasons no operator could
trace back.

## Public-repo note

The unscoped refusal payload carries a `candidates` list of real registered repository paths. It is
useful to an operator and stays in the runtime output, but it must not be pasted into a doc, a
commit message, or a test fixture — P18/HR34, and the lesson of `e909fdf`.
