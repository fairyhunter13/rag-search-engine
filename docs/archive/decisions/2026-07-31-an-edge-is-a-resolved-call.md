# An edge is a resolved call, or it is not an edge

**2026-07-31** · P6/HR15, P8 · `EXTRACTOR_REV` e7 → e8 · guard: `test_extraction_phase5.py` TS10 / TS10b

The stored call graph was 95.7% wrong, and nothing in the suite could tell. This records why the
fan-out cap is **1** and not a number chosen off a curve, why the fix is a *scope preference*
rather than an exclusion, and what the 19× drop in fleet edges does and does not mean.

## The defect

`_extract_graph` resolved a call by name within a language family and emitted an edge to **every**
candidate. One call site bound to N definitions, and `graph()` presented all N with equal
confidence — no confidence column, no `ORDER BY`, no way for a reader to tell a resolution from a
guess. Grouping every stored edge by `(caller, callee-name)` — the exact candidate set the fan-out
walked — across 155 stores:

| edges whose group has fan-out | count | share |
|---|---:|---:|
| 1 (unambiguous) | 100,556 | 4.3% |
| 2 | 50,588 | 2.2% |
| 3–4 | 53,625 | 2.3% |
| 5–8 | 88,861 | 3.8% |
| 9–32 | 398,195 | 17.2% |
| **>32** | **1,628,965** | **70.2%** |

2,220,234 of 2,320,130 edges were ambiguous. Against at most 185,663 correct ones, fleet precision
was **≤ 8.0%**.

This is the *inverse* of the S0 defect. S0 recorded that `callee_file != fstr` was a bug: it
discarded every call whose target was defined in the same file, so `callers`/`callees` could not
answer any relation that stayed inside one file. Restoring same-file calls was right, and it left
this. The repair is not to re-exclude same-file — it is to make same-file the **preferred scope**,
which is what every language's scoping rules actually do.

## Why the cap is 1

Two tiers: candidates in the caller's own file, else all candidates in the family. Take the first
non-empty tier, drop the caller from it, emit only if exactly one candidate remains.

The rule originally proposed for picking that threshold was *"the largest cap where median
modularity Q still improves and median `impact` stays under the query layer's `LIMIT 50`."* It
degenerates: Q improves at **every** cap, and more the lower the cap, and median `impact` is under
50 at every cap too. Both clauses hold everywhere, so the rule selects the largest candidate — 16,
the worst option on precision. Neither clause discriminates.

What discriminates is precision. Model each call site as one `(caller_sid, callee_name)` group;
exactly one member of the group's pool is the true callee, because that is what a resolved call
*is*. Then, over 155 stores and 193,309 groups:

| cap | edges emitted | correct | precision | recall | F1 | F0.5 | wrong edges |
|---:|---:|---:|---:|---:|---:|---:|---:|
| **1** | **122,324** | 122,324 | **1.000** | 0.633 | 0.775 | **0.896** | **0** |
| 2 | 157,854 | 140,089 | 0.887 | 0.725 | **0.798** | 0.849 | 17,765 |
| 3 | 178,704 | 147,039 | 0.823 | 0.761 | 0.791 | 0.810 | 31,665 |
| 4 | 193,176 | 150,657 | 0.780 | 0.779 | 0.780 | 0.780 | 42,519 |
| 8 | 254,772 | 160,432 | 0.630 | 0.830 | 0.716 | 0.662 | 94,340 |
| 16 | 400,875 | 172,765 | 0.431 | 0.894 | 0.582 | 0.481 | 228,110 |
| none (tiers only) | 1,021,043 | 185,663 | 0.182 | 0.960 | 0.306 | 0.217 | 835,380 |

The structure is exact and worth stating on its own: **each step from cap C−1 to C admits groups of
size C, which contribute one correct edge and C−1 wrong ones.** The marginal edge bought by raising
the cap to C is correct with probability `1/C` — a coin flip at 2, one-in-four at 4. Cap 1 is the
only value at which both the table and the marginal edge are correct by construction.

F1 peaks at cap 2, and F1 is the wrong prior here. It weights precision and recall equally, which
would be right for a ranked list a consumer can threshold. This table is read by `callers`/`callees`
with no ordering and no confidence, so a wrong edge is indistinguishable from a right one at the
point of use. F0.5 peaks at 1. Under cap 1 an edge in the table *means* something, and that is a
statable invariant the query layer can rely on without being changed first.

Note also that the tiers alone, with no cap, already cut 2,320,130 → 1,021,043 (56%), because the
old code applied no scope preference at all.

## The tier is chosen before the caller is dropped, and the orderings disagree

Compute the same-file tier *including* the caller, then remove the caller from whichever tier won.
The other order — drop self first, then pick a tier — makes a recursive call in the file holding
the only local definition find an empty tier and fall through to a same-named definition in
**another file**. That is a confidently wrong edge, and it is worth 996 fleet-wide.

This was found by accident. The first projection inferred candidate sets from *stored* edges, which
have self-edges already removed, and over-counted by exactly those 996. Reconstructing candidates
from the `symbols` table directly is what surfaced the ordering as a decision rather than a detail.
TS10b pins it.

## Two tiers, not three

A same-directory middle tier was measured and declined. At cap 1 it converts +5,697 groups (+4.7%)
from ambiguous to resolved — a *larger* relative gain than the 2.5% it bought at cap 4, because a
middle tier helps most exactly when the cap is tightest.

It stays out because same-file resolution is what a language's scoping rules do, whereas directory
proximity is a guess. Under cap 1 a tier that narrows to the *wrong* single candidate emits a
confidently wrong edge into a table whose entire new invariant is that it holds none. The
measurement is recorded here so the option is costed rather than forgotten: +5,697 groups.

## What is lost, and why it cannot be recovered here

Recall falls to 0.633. That 36.7% is not recoverable at the resolution layer: `extract_calls_with_lines`
returns bare `(name, line)` with no receiver, so nothing downstream can tell `$a->save()` from
`$b->save()`. Closing it requires qualifying the *call site* in the extractor. That is a separate
change with its own stamp; it is named here, not attempted.

Caller retention is language-skewed — php 68.4%, java 79.8%, tsx 80.2%, javascript 81.5%, go 87.5%,
python 87.5%, typescript 90.4%. php is both the largest corpus and the most ambiguous. Expected,
and stated so the next reader does not file it as php-specific breakage.

## The gap this leaves, stated rather than papered over

Emission now depends on how many definitions share a name *globally*, so on the watcher's
incremental path a newly added definition can turn a resolved call ambiguous for call sites in
files that pass never re-walks — and the now-wrong edge survives until the next full re-derive. The
unconditional fan-out had the same shape of gap for the same reason. The repair is reconcile, not a
wider walk during extraction.

## Verification

**Equivalence before effect.** Old and new `_extract_graph` were run over the *same pristine tree*
(a detached worktree at the parent commit), comparing by `name|file|start_line` rather than `sid` —
`symbol_id` hashes shift between checkouts, and an earlier attempt that compared a live store
against a fresh derive was meaningless for exactly that reason.

```
symbols 1,984 -> 1,984      (unchanged)
edges   4,688 -> 2,694      (57.5%), a strict SUBSET: 0 additions
removals 1,994, unexplained 0
  1,447  cap: tier still ambiguous
    501  lost to the same-file tier
     46  empty tier after self-drop
max (caller, callee_name) fan-out  20 -> 1
```

The "unexplained" predicate was wrong twice before it was right, and both errors flattered the
change: checking only the cap left 5 false unexplained (the recursion cases), and asking whether
the *group* resolves rather than whether *this edge* survives left 501. It reaches 0 only when the
question is per-edge.

**The gate that did not exist.** Nothing failed while the graph was 96% ambiguous — which is why
this survived to a backlog. TS10 asserts no `(caller, callee_name)` group in a freshly derived
store exceeds `_MAX_CALLEE_FANOUT`, and TS10b that recursion does not fall through to another
file's homonym. Both fail against the parent commit; without them the next resolution change
silently reintroduces the fan-out and every other test still passes.

Suites: 865 fast, then 887 including `costly` and `exclusive` — 0 failures. The browser suite could
not run: the optional `browser` extra is not installed, which is pre-existing and not gated by the
default CI jobs.

## The fleet result

Predicted from the symbol tables before the change, measured after:

| | before | predicted | measured |
|---|---:|---:|---:|
| edges | 2,320,130 | ~122,324 | **122,475** |
| edges per symbol | 6.146 | 0.324 | **0.324** |
| median modularity Q | 0.571 | ~0.769 | **0.752** |
| stores gaining `symbol_hollow` | — | exactly 1 | **exactly 1** |
| degenerate stores | 3 | 3 | **2** |
| distinct stamps | — | 1 | **1** |

**A 19× drop in edges is the intended result, not a regression.** Anyone reading `edges_per_symbol`
against its history will see the largest single fall the metric has recorded; this is the reason.

One store — 6 symbols, 1 edge — is driven edge-free and now reports `symbol_hollow`. That is an
accurate report of a graph whose only call is ambiguous, not a regression to suppress. The
degenerate count came in one *below* the projection.

## The pass merge, and a speedup that was smaller than filed

`_extract_graph` walked the tree twice and paid `run_bounded` twice per file, because callee
resolution needs the whole symbol table and that does not exist until the first walk finishes. The
ordering constraint is real; a second *IPC round trip* was never part of it.
`extract_symbols_calls_with_stats` returns both halves in one trip and the caller buffers the call
sites (~11 MB on the largest project in the fleet: 2,516 files, 94,867 call sites, against a daemon
already resident at ~1 GB).

Filed as 25–30%, inferred from "IPC is 3.9 ms of 13.7 ms per file". That assumes the second trip
costs what the first does. It does not — the second pass only parsed calls, and already skipped
every file with no symbols. Measured against an otherwise-identical checkout, 2,319 files, median
of 3 reps:

| tree | files | before | after | Δ |
|---|---:|---:|---:|---:|
| this repo (python) | 228 | 35.13 ms/file | 35.18 | **0.0%** |
| a php service | 495 | 28.54 | 26.36 | −7.6% |
| a js service | 571 | 12.30 | 11.81 | −4.0% |
| an android/java app | 727 | 13.90 | 9.55 | **−31.3%** |
| a ts suite | 298 | 13.84 | 12.80 | −7.5% |
| **all five** | 2,319 | 43.39 s | 38.57 s | **−11.1%** |

≈11%, and the spread matters more than the average: the win is large where symbol extraction is
cheap relative to the call parse, and vanishes where it is not. Don't quote a single number.

It ships in the same commit and stamp window as the resolution change — a stamp move re-derives
every graph in the fleet, so shipping them apart buys the same re-derive twice — but it was
**diffed separately**, and produces a byte-identical edge set. That separation is not optional:
without it, neither change's effect is attributable.

## The stamp

`EXTRACTOR_REV` e7 → e8. **`ALGO_VERSION` stays `fg3`** — the partition changes because its input
changed, not its algorithm, and restamping it would claim otherwise.

The bump is mandatory rather than conventional. Call resolution lives in `daemon/sweeps.py`, which
is deliberately outside `_FINGERPRINT_MODULES` (hashing a module that large would re-derive the
fleet for an unrelated log line), so `_code_fingerprint` cannot see this change at all. Without the
bump it would have served stale edges forever. `_MAX_CALLEE_FANOUT` sits next to
`_FINGERPRINT_MODULES` for edit locality only, and carries that warning inline.

### An operational hazard found while doing this

`_code_fingerprint` reads the module bytes off disk on **every call**, so a running daemon sees a
new fingerprint the moment `graph/extractor.py` is saved — before any commit, and even for a
docstring. During this work one store was re-derived with the daemon's old in-memory code and
stamped with the new fingerprint. It self-healed at the e8 bump, because that changes the stamp
again for everything.

The general rule: **hold a sweeps pause lease across an edit session that touches a fingerprinted
module**, not just across the re-derive. And releasing it schedules nothing — reconcile is
startup-once, and moving `EXTRACTOR_REV` touches no file so fires no watcher event. Release the
lease *then* restart, and verify by watching the stale count fall rather than by the absence of
errors.

## The follow-up this exposes but does not fix

`callers`/`callees` truncate at 50 with no `ORDER BY`, and `impact` runs a depth-5 BFS with no
bound on the visited set. The cap takes the share of symbols exceeding that truncation point from
11.7% to 0.0%, which makes the defect survivable — it does not remove it. It is a query-layer
change, needs no stamp, and mixing it into a re-derive commit would make the equivalence diff
unreadable.
