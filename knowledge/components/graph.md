---
type: Component
resource: src/rag_search/graph/
title: "graph: symbols, edges, and communities"
description: Tree-sitter extraction, the call graph, and the community partition — plus EXTRACTOR_REV, the human-moved stamp that decides whether the fleet's graphs are stale.
tags: [graph, extractor, community, extractor-rev, php, quality]
status: active
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# graph: symbols, edges, and communities

Five files, ~2,200 lines, `extractor.py` more than half of it. Structure comes from the grammar and
nothing else — see
[structure is read, never classified](../constraints/structure-is-read-never-classified.md).

## `EXTRACTOR_REV` is a contract revision, moved by hand

It names what extraction *emits*, not what the file says. Together with `community.ALGO_VERSION` and
a source fingerprint of the extraction modules it composes the pipeline identity that
`daemon/sweeps.py` compares against every store's `graph.db`.

Two failure modes sit either side of it:

- **Change output without bumping the rev** and every existing graph stays stale while reporting
  itself current. No test can catch this — the bump is a human step.
- **Bump the rev** and the whole fleet re-derives. The daemon re-reads the fingerprinted modules on
  every call, so a *saved* edit to `extractor.py` restamps immediately; a docstring counts.

The suite holds the pair either side of the stamp rather than the stamp itself: TS3 pins the
identity's composition, TS0/TS8 pin what extraction emits. Log of past moves:
[the extractor rev log](../../docs/decisions/2026-08-14-the-extractor-rev-log.md).

## The PHP circular import is deliberate

`php_receivers.py` imports helpers from `extractor.py` at module level; `extractor.py` imports
`php_receivers.parse_facts` **lazily, inside a function**. That asymmetry is the cycle-breaker — the
lazy side must stay lazy.

An edge is emitted only when the receiver evidence narrows candidates to **exactly one** symbol. The
`_MAX_CALLEE_FANOUT` drop is therefore precision-preserving, not a bug to fix: a call that could go
to twenty methods contributes nothing rather than twenty guesses. See
[an edge is a resolved call](../../docs/decisions/2026-07-31-an-edge-is-a-resolved-call.md).

## Modularity Q alone is never the gate

`quality.py` gates on a composite — `singleton_ratio` plus coverage plus Q — because sparse graphs
have exponentially many near-optimal partitions and Q cannot tell them apart. A gate on Q alone
passes a degenerate partition.

## Guards

| Claim | Guard | File |
|---|---|---|
| the rev is part of graph identity; an ambiguous call emits nothing | `test_ts3_extractor_rev_is_part_of_graph_identity`, `test_ts10_ambiguous_calls_emit_nothing_and_same_file_wins` | `test_extraction_phase5.py` |
| a call site never becomes a definition | `test_gt3_a_call_site_never_becomes_a_definition` | `test_extraction_ground_truth.py` |
| a degenerate partition is caught | `test_degenerate_fires_on_all_singleton_graph` | `test_hierarchy_quality.py` |
