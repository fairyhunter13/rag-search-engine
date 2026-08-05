# Guards on a function nothing calls

**2026-08-05** · HR28 / HR35 / HR38 · mechanism: `daemon/sweeps.py` `_source_fingerprint`

Earlier the same day, `2026-08-05-a-fleet-number-behind-a-project-scoped-gate.md` recorded that
AU5 had guarded `_pipeline_block`'s arithmetic for five days while nothing could reach it. The
generalisation is mechanical, so it was worth asking mechanically: **which private helpers have no
production caller at all?**

## The measurement

AST for definitions, word-boundary matching for uses, across `src/rag_search` — 210 uniquely-named
private functions. **Two** had zero production callers.

`_graph_lane_join` is a false positive **by design**, and its docstring says so: the heavy-pass
completion guarantee "is what the lane moved, not what it removed — this hands it back to anyone
who needs it (the WG gates read the graph the moment `on_change` returns)". A synchronisation
affordance for the suite, living in the module it synchronises. Left alone.

`_source_fingerprint` was real.

## What was dead

HR38 (2026-07-01) repointed every drift gate to the code-only `_code_source_fingerprint`. All five
production call sites took it. `_source_fingerprint` — the all-files walk — kept none, and the last
non-test caller left with tier 3 on 2026-07-28. Also dead: `_fingerprint_cache`, which only it read,
and the `_fingerprint_cache.pop(...)` in `on_change`, invalidating a memo for nobody on every
filesystem event.

Four guards across three files went on asserting its behaviour, and **two of them were the named
guards for live invariants**:

- **DIS5** (`test_idle_stability.py`) — HR35's hidden-dir case. The guard for the 2026-07-01
  incident that pinned a CPU core for hours on tool-cache churn. Repointed; the property holds on
  the live gate, which routes through the same `_should_drop` resolver. It was simply unwatched.
- **GG4** (`test_docs_index.py`) — HR28's docs case. This one was worse than unwatched.

## GG4: the property was real, the function was not

GG4 asserted that adding a docs file *moves* `_source_fingerprint`, with the stated reason that a
prose change must be "*seen* — otherwise it would never be re-embedded".

`_code_scan` does `if not is_code_language(detect_language(f)): continue`. **A docs write does not
move the live gate, and never did.** Docs freshness is delivered somewhere else entirely: the index
step in `on_change` runs *unconditionally and above* that gate.

So HR28's real property is an **ordering** invariant — `_index_files` above the sig gate — and
nothing asserted it. Hoisting the gate (a plausible optimisation: "why index when nothing changed?")
would silently stop every docs file in the fleet from being re-embedded, with the whole suite green.

GG4 now calls `on_change` and asserts the index step is reached on a docs-only event.

## The negative control, and what it caught

Injecting exactly that regression — the sig gate hoisted above the index call — the **first**
rewrite of GG4 still passed. The gate cannot fire on a project it has never seen: `_last_labelled_sig`
is unset, so any sig differs and indexing proceeds. The regression bites on the *second* event for
an unchanged code tree, which is precisely what a docs edit is.

GG4 therefore establishes a baseline pass first, and asserts the drift gate is stamped before the
docs event is sent. With that, it fails against the injected regression and passes against `main`.

Worth stating plainly: a guard written against the right function, for the right reason, was still
not a guard until it was run against the failure. That is the third time this week the same lesson
has been paid for.

## Symmetry worth keeping

FCG1 (`test_idle_stability.py`) is GG4's mirror and deliberately stubs `_index_files` away to assert
the *negative* — docs churn must not wake the graph lane. That stub is why FCG1 could never have
noticed the positive half was unguarded. Both halves of HR38's docs behaviour are now watched; before
this, only the one that saves CPU was.

## Method note

The detector is six lines of `ast` and is not checked in. Standing guards are better than periodic
sweeps, and a tree-wide "no orphaned private helper" rule would fail on `_graph_lane_join` for good
reasons — an allowlist maintained against a two-item population is more liability than the check is
worth. Re-run it by hand after a subsystem is retired; that is when the population changes.

One correction it did *not* catch on its own: it scans function definitions, so
`_fingerprint_cache` — a module-level dict — survived the first pass and was found by grep. A
follow-up sweep should cover module globals too.
