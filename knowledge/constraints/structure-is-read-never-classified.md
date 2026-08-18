---
type: Constraint
resource: src/rag_search/graph/
title: Structure is read, never classified
description: HR15, HR24, HR3, HR20 and HR25 — no regex or keyword table may decide what user code means, community detection is deterministic and flat, the partition-quality gate is composite, and the pipeline re-derives itself from version stamps.
tags: [tree-sitter, community-detection, igraph, determinism, hr3, hr15, hr20, hr24, hr25]
status: active
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# Structure is read, never classified

Everything the engine knows about a repository's shape comes from a parse tree and a graph
algorithm. Nothing comes from a table of framework names, and — since the 2026-07-28 purge — there
is no LLM left to fall through to either. That closed door is the point: a heuristic tier exists
mostly to rescue a doctrine that has an escape hatch.

## The doctrine, and its two exemptions (HR15)

Nothing in the package may classify what the user's code means by regex, keyword list or mapping
table. Two modules are exempt **by name** in `_CATEGORY_B_ALLOWLIST` — `core.registry` (tier-suffix
strip) and `core.config` (project-name slug) — because neither reads user code; they read this
engine's own naming.

Every other module is screened against the wide pattern set
(`compile|finditer|findall|search|match|fullmatch|sub|subn`), measured at zero hits outside the two.

The allowlist carries **its own dead-entry check**, because an exemption for a module that no
longer uses the thing exempted is a hole nobody can see. The guard also derives its module set by
walking the package rather than listing names — see
[a guard test named its own modules](../defects/a-guard-test-named-its-own-modules.md) for what
happens when it does not.

## Detection is flat, seeded, and byte-identical (HR24)

`detect_communities` runs `igraph.community_fastgreedy().as_clustering()` — Clauset-Newman-Moore —
over the undirected subgraph of the symbol call graph. Edgeless symbols are grouped **by directory**
rather than left alone, which is what avoids an N-singleton explosion on a repo with few resolved
calls.

The RNG is seeded at module import (`random.seed(0)` plus `igraph.set_random_number_generator`) so
two runs from the same graph produce identical output. `leidenalg` must not be imported here.
`ALGO_VERSION = "fg1"`, and all output rows are `level=1` — there is no L2 or L3 hierarchy; both
were built and deleted.

## Re-running must not destroy work (HR3)

`upsert_community` passes `summary=None`, never `""`, so re-detection leaves an existing summary
alone and `ready` stays `ready` across a re-index. The distinction between "no new summary" and
"an empty summary" is the whole guarantee.

## The readiness gate is composite, deliberately (HR20)

`partition_quality(store)` is deterministic — igraph plus SQL, no inference — and computes
`modularity_q`, `coverage` (intra-community edges over total) and `singleton_ratio`. A partition is
degenerate when:

```
edges>0 AND (singleton_ratio ≥ 0.60  OR  coverage < 0.20  OR  (n_l1 ≥ 2 AND modularity_q < 0.05))
```

**Modularity Q alone is explicitly rejected** as the sole gate: exponentially many near-optimal
partitions exist and Q degrades on sparse code graphs.

Every clause requires `edges > 0`, so an **edge-free project is exempt from the entire gate**. A
repo with no resolved calls cannot structurally form non-singleton communities, and penalising it
would report a property of the language rather than of the repo.

A degenerate partition demotes `index_state` from `ready` to `degraded`, and this gate is now the
only thing between `indexing` and `ready` — which is why the ladder is three-valued rather than
four. See [retrieval is recall then rerank](retrieval-is-recall-then-rerank.md) for the rest of that
field's contract.

## The pipeline heals itself from two stamps (HR25)

`graph.db` carries a `meta(key, value)` table that survives `GraphStore.clear()`, holding:

- `algo_version` = `f"{ALGO_VERSION}+{_code_fingerprint()}"` — the code contract
- `source_sig` — a SHA-1 over the code files' `relpath:mtime`, stat-only and GPU-free

Both are stamped by `_index_project` after detection and by `_rederive_graph` after a re-derive.
`reconcile_projects` compares them and picks one of three actions — full index plus label when there
are no communities at all, re-derive plus label when the graph is stale, and **label-only** when
just the summaries are missing — so a settled project is never re-indexed to fix a label. It rides
the existing 30-minute reconcile loop; no new timers.

`_rederive_graph` never calls `get_embedder` or `embed()`, which is what keeps this whole path off
the GPU.

**Two operational traps live here.** `_code_fingerprint` re-reads the fingerprinted modules from
disk on every call, so a running daemon restamps the moment `graph/extractor.py` is *saved* — no
commit needed, and a docstring counts. And releasing a sweeps pause lease schedules nothing: the
reconcile pass is startup-once and then parks. Both are worked through in
[moving the pipeline stamp](../runbooks/moving-the-pipeline-stamp.md).

## Sources

Rows HR3, HR15, HR20, HR24 and HR25 in
[§13b](../../docs/architecture/federation-ops-and-invariants.md).

| Row | Guard | File |
|---|---|---|
| HR3 | `test_detect_communities_idempotent` | `test_graph.py` |
| HR15 | `test_no_code_semantic_regex_outside_allowlist`, `test_category_b_allowlist_has_no_dead_entries` | `test_no_code_semantic_regex.py` |
| HR20 | `test_edge_free_graph_not_degenerate`, `test_degenerate_fires_on_all_singleton_graph` | `test_hierarchy_quality.py` |
| HR24 | `test_sc8_no_leidenalg_in_community`, `test_sc8_detect_communities_deterministic` | `test_schema_consistency.py` |
| HR25 | `test_algo_drift_triggers_rederive`, `test_source_drift_triggers_rederive`, `test_rederive_graph_has_no_embedder_call` | `test_daemon.py` |

Records: [the extraction track is closed](../../docs/decisions/2026-08-03-the-extraction-track-is-closed.md),
[an edge is a resolved call](../../docs/decisions/2026-07-31-an-edge-is-a-resolved-call.md),
[atomic graph re-derive](../../docs/decisions/2026-07-31-atomic-graph-rederive.md).
