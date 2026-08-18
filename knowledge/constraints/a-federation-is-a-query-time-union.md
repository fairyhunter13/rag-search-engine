---
type: Constraint
resource: src/rag_search/daemon/federation.py
title: A federation is a query-time union, never a merged index
description: HR4, HR5 and federation invariants #1–#4 and #6–#8 — members keep their own stores, the union happens per query, and one absolute path maps to exactly one index directory.
tags: [federation, registry, index-isolation, hr4, hr5]
status: active
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# A federation is a query-time union, never a merged index

A "federation root" is a repo containing symlinks to other repos. The tempting implementation is to
walk through the symlinks and index everything into the root. This repo does the opposite, and the
distinction is the whole subsystem.

## The rule

Each member is indexed as **an independent project with its own `graph.db` and its own vector
store**. The root's index contains no member symbol. A query scoped to the root expands through
`expand_federation` at request time and unions the answers.

The load-bearing consequence: **there are no cross-repo edges anywhere in the system.** A call graph
never spans two repos, so no member can be made stale by another member's re-index, and removing a
member is a deletion rather than a re-derive.

## One absolute path, one index dir (HR5)

`index_dir(path)` is keyed on the absolute path. Two clones of the same upstream are two projects
with two indexes, deliberately — they can be at different commits, and treating them as one would
make every answer about the pair unfalsifiable.

The corollary caught a real bug. `_owning_root` maps a filesystem event to the project that owns it,
and the first implementation used `path.startswith(root)`. That has **no path-boundary check**, so a
root named `foo` is a string prefix of a sibling named `foo-bar` and events could be attributed to
the wrong project depending on set iteration order — corrupting exactly this isolation. Fixed
2026-07-09 to a longest-`Path.relative_to`-match, guarded by
`test_wt5_prefix_sibling_roots_no_misattribution`.

## The seven invariants, and what each is really asserting

| # | Invariant | The failure it names |
|---|---|---|
| #1 | No inlining | a member's symbols appearing in the root index |
| #2 | Members are first-class | a member that answers only through its root is an inlined shard |
| #3 | `root.federation` is authoritative | discovery and the registry disagreeing after a rerun |
| #4 | Logical-repo coverage | `search([root])` missing content the root can reach |
| #6 | Forbidden roots | `/tmp` or `~/.cache` registered as a project |
| #7 | Idempotency | discovery, registration, reconcile and repair diverging on rerun |
| #8 | Registry ↔ storage consistency | `projects.json` and `INDEX_ROOT` drifting apart |

#2 is the one most often mis-asserted. It used to be proved with an *unscoped* search — "findable
without naming any project" — but that fleet-wide fallback was deliberately deleted, so the test was
asserting the absence of a removed feature. The honest reading under scatter-gather is that a member
is reachable **both** through its root's union and on its own path; both arms are checked, and the
second is not redundant.

Duplicate symlinks to the same member dedup at three layers — `discover_members` at the source,
`index_members` when storing `root.federation`, and `expand_federation` as defence — because a
member counted N times is an N× storage bloat that no single-layer check catches.

## Sources

Rows HR4 and HR5, and the §13c table, in
[§13b/§13c](../../docs/architecture/federation-ops-and-invariants.md).

| Claim | Guard | File |
|---|---|---|
| #1, #3, #8, HR5 | `test_inv1_no_inlining`, `test_inv3_federation_authoritative`, `test_inv8_cascade_remove` | `test_federation_architecture.py` |
| #2 | `test_inv2_members_first_class` | `test_federation_architecture.py` |
| #4, HR4 | `test_inv4_root_scoped_search_fanout`, `test_inv5_graph_definition_fanout`, `test_inv7_overview_status_aggregates` | `test_federation_logical_entity.py` |
| #6 | `test_inv6_forbidden_root`, `test_index_tool_rejects_forbidden_root` | `test_federation_architecture.py` |
| dedup | `test_gdup_duplicate_symlink_members_deduped` | `test_federation_architecture.py` |

Related: [the MCP tool surface](../interfaces/the-mcp-tool-surface.md) fans out through this union
on every scoped call, and
[descriptor exhaustion in the federation fan-out](../../docs/decisions/2026-07-29-descriptor-exhaustion-in-the-federation-fanout.md)
records what that fan-out cost at 139 members.
