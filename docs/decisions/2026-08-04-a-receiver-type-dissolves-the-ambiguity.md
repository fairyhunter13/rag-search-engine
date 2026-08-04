# A receiver type dissolves the ambiguity rather than trading precision for it

**Date:** 2026-08-04 · **Stamp:** `EXTRACTOR_REV` e12 → e13 · **Module:** `graph/php_receivers.py`

## What shipped

PHP call sites now carry the receiver's *declared* type, and `_extract_graph` uses it to narrow an
ambiguous callee pool **before** `_MAX_CALLEE_FANOUT` drops the edge. Three hops, all tree-sitter,
no new dependency, no install, no network:

1. the receiver's type from the same file — a typed property, a typed parameter, `$v = new X()`,
   `$this`, `self`/`static`/`parent`;
2. that class name to an FQN via the file's own `namespace` and `use` clauses, and the FQN to a
   file via the PSR-4 map `_ImportResolver._read_psr4` already builds for `file_imports`;
3. failing that, upward through `extends` and trait `use` (depth 8, cycle-guarded), accepting only
   if exactly one class on the chain declares the callee.

## The cap did not move, and that is the whole design

`_MAX_CALLEE_FANOUT` is still 1. A pool of five that a declared type cuts to one is not the cap
relaxed to five — it is a pool of one, qualified by evidence the name-only tier never consulted,
clearing the existing bar on its own terms. Above the cap and unnarrowed, the edge is still
dropped. So the recall/precision table in `sweeps.py` still describes this code: every emitted edge
means a resolved call, and `graph_handler` still has no confidence column it would need to say
otherwise.

## Why it was missed for so long

The tier was struck once on measurement from a CodeIgniter 3 store — *"php: type declared 0.0%,
chained 80.4%"*. CI3 predates namespaces, `use`, and type declarations, so it was the one
population where these hops were guaranteed to read zero. 400-file samples say the fleet is not
that store:

| | Laravel (13,272 files) | CI3 (6,272 files) |
|---|---:|---:|
| files declaring a namespace | 78.2% | 0.0% |
| typed parameters | 65.1% | 8.8% |
| typed properties | 79.6% | 0.0% |
| chained `$this->a->b->m()` — *the stated blocker* | 0.0% | 1.4% |

## Measured before it was built, fleet-wide

`scripts/probe_php_receivers.py`, read-only, over all 105 PHP roots — 22,842 files, 585,970 call
sites. It drives the shipped resolver rather than a copy of it, so the prediction and the
implementation cannot drift.

| | files | sites | resolve today | dropped | recovered | % of dropped | direct | chain |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ci3 | 6,272 | 264,293 | 68,138 | 40,576 | 6,520 | 16.1% | 510 | 6,010 |
| laravel | 13,272 | 261,514 | 29,233 | 17,018 | 4,367 | 25.7% | 3,490 | 877 |
| other | 3,298 | 60,163 | 14,375 | 6,323 | 2,507 | 39.6% | 2,453 | 54 |
| **ALL** | **22,842** | **585,970** | **111,746** | **63,917** | **13,394** | **21.0%** | 6,453 | 6,941 |

The gate was ~20% of dropped sites recovered. It clears at 21.0% fleet-wide, and per root 18 of the
29 roots with ≥200 dropped sites clear it — Laravel 8/10 at a 37.4% median, `other` 8/9 at 34.4%.
The win is not concentrated: the top 5 roots hold 50% of recovered edges across 105.

**The single-store headline did not replicate.** The plan's pilot read 43.6% of dropped recovered
and +37.4% edges; fleet-wide it is 21.0% and +12.0%. That is this lineage's standing rule landing on
its own plan — seven of eight imported headline numbers have now failed local replication, and this
one was imported from a single store of my own.

## The self-check passed, and it passed for a reason nobody predicted

CI3 was supposed to read near zero on hops 1–2, and it does: **510 direct recoveries out of 40,576
dropped, 1.3%**, against Laravel's 3,490. Had CI3 read high, the probe would have been wrong rather
than the corpus.

What was not predicted is where CI3's 16.1% comes from instead: the **inheritance walk**, resolving
`$this->m()` and `self::m()` onto `CI_Controller` and `MY_Model`. 6,010 edges that no
type-declaration tier would ever have found — a dialect with no types at all is still carried by a
hop that reads only `extends`. Laravel inverts the ratio exactly (3,490 direct, 877 chain).

## The bug that would have shipped a 40% smaller win, found by measuring the join

The hints are only ever used joined back onto `extract_calls_with_lines`' output by
`(callee name, line)`. `php_receivers` originally derived that pair itself — the `name` field, and
the name node's own line. Measured against the shipped extractor on 105 roots, **only 44–87% of
call sites joined**, and one root managed 6.1%.

Two independent causes, both invisible without the measurement:

- PHP spells the callee field of a plain call `function`, not `name`, so **every bare `env(...)`,
  `is_dir(...)`, `json_encode(...)` in the fleet was missing** from the hint side.
- `_collect_calls_with_lines` records the *call node's* start line; a name that wraps onto a later
  line disagrees.

The fix is not a better re-derivation — it is not re-deriving at all. `php_receivers` imports
`_callee_node`, `_is_call_node` and `_unwrap_callee` from `extractor` and keys on exactly what the
sweep emits. The join is now **100% on all 105 roots**, by construction rather than by coincidence.
This is also why the probe's own totals moved between runs (585,970 sites, not 324,928): it had been
measuring a call population 45% smaller than the one the sweep resolves.

Where two calls to the same name share one line, the key is dropped rather than guessed — the
cap's own rule applied one level earlier, because a hint belonging to the *other* call would narrow
to a confidently wrong edge, the one outcome worse than the drop this tier exists to undo.

## P6/HR15: nothing here interprets a name

A written type annotation, a `use` clause, a PSR-4 prefix in `composer.json`, and an `extends`
edge are all **declared facts**, structurally present in the tree or in a manifest. There is no
regex, no keyword list, and no mapping table from names to meanings. A step that names no file
resolves to nothing rather than to a guess — which is why CI3's `$this->load->model('x')` is
explicitly *not* here: recognising it needs a closed set of framework method names, which is the
HR15 question this item deliberately does not open.

## Stamp and fingerprint

`sweeps.py` is not in `_FINGERPRINT_MODULES`, so a resolution change there is invisible to
`_code_fingerprint` and would serve stale edges forever. `graph/php_receivers.py` was **added** to
`_FINGERPRINT_MODULES`, and `EXTRACTOR_REV` went e12 → e13 in the same commit.

The parse runs inside the existing bounded-pool trip rather than beside it, as a fifth element of
the extraction tuple. Pulling it out would mean a second IPC round trip for every PHP file in the
fleet; doing it unbounded in the daemon would put a grammar segfault — measured, `process()` on a
10,000-deep expression — inside the process that must not die.
