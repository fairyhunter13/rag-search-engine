---
type: Constraint
resource: src/rag_search/query/search.py
title: Retrieval is recall then rerank
description: HR8, HR7 and HR30 — stage one fuses vector and BM25 on rank, stage two reranks on the GPU, results are ordered by rerank score only, and the surface that exposes them is exactly four tools.
tags: [retrieval, rrf, reranker, index-state, mcp, hr7, hr8, hr30]
status: active
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# Retrieval is recall then rerank

Two stages, and the split is the design.

**Stage 1 — hybrid recall.** A bi-encoder vector search over `sqlite-vec` and an FTS5 BM25 search
over `chunks_fts`, fused by Reciprocal Rank Fusion.

**Stage 2 — cross-encoder rerank.** `gte-reranker-modernbert-base` on the GPU, over both AXIS A
(code chunks) and AXIS B (community summaries). AXIS B has no lexical lane — a summary has no
literal tokens a user would search for.

Results are ordered by `rerank_score`, **never by the bare retrieval score**. Reranking never runs
at index time; it is a query-time cost paid on a candidate set, which is the only reason a
cross-encoder is affordable at all.

## Why fusion is on rank, not score

Cosine similarity and BM25 share no scale and no polarity. A weighted sum of the two is arithmetic
on incomparable quantities — it produces a number, and the number means nothing. RRF uses only the
position each lane assigned, which is the one thing the two lanes agree about.

## The fan-out is threaded, and why that is safe

`_FANOUT_WORKERS = 8` threads query members in parallel. That is safe only because both lanes are
pure sqlite — which releases the GIL — and the GPU is touched exactly once before the loop and once
after it. It must never contend with the embedder's `_GPU_INFER_LOCK`. Each member holds its own
`check_same_thread=False` connection, and no two workers share one.

## `index_state` has three values and no fourth (HR7)

`indexing → degraded → ready`, ranked in `server/_overview.py::_rank`. `ready` requires that
vectors exist *and* that the partition-quality gate says the partition is not degenerate — see
[structure is read, never classified](structure-is-read-never-classified.md).

There is deliberately **no fill-rate rung**. Structural labelling fills every summary
deterministically in one pass, so such a rung could only ever read 0 % or 100 % and would
discriminate nothing. A federated entity takes the worst state among its members.

## The surface is exactly four tools (HR30)

`index`, `search`, `graph`, `overview`. The guard asserts **set equality**, not a subset: a subset
check cannot see an *extra* tool, and "exactly four" is the claim. `mcp.list_tools()` is the
registry; a static mirror list must not be added beside it.

Three refusals on that surface are load-bearing rather than defensive, and all three fail loud
because the quiet version is worse:

- **`search` with no `project_paths` fails**, and does not fall back to "all projects" — measured at
  164.78 s against 7.01 s, and answering out of the wrong repo besides.
- **An unknown `scope` is rejected**, because `scope_languages` maps an unknown value to "no
  restriction", which silently *widens* the corpus.
- **`index()` canonicalises only**, never resolving to an enclosing root, or a child directory gets
  registered under its parent.

Full signatures: [the MCP tool surface](../interfaces/the-mcp-tool-surface.md).

## Sources

Rows HR7, HR8 and HR30 in
[§13b](../../docs/architecture/federation-ops-and-invariants.md).

| Row | Guard | File |
|---|---|---|
| HR7 | `test_overview_status_has_index_state`, `test_index_state_demoted_when_degenerate` | `test_server.py`, `test_hierarchy_quality.py` |
| HR8 | `test_e1_rerank_reorders_search_results`, `test_e2_ask_context_is_rerank_ordered`, `test_e3_community_context_is_reranked`, `test_e4_rerank_lift_metric` | `test_server.py` |
| HR30 | `test_mcp_has_four_tools` | `test_server.py` |

Records: [the retrieval eval harness](../../docs/decisions/2026-07-31-retrieval-eval-harness.md),
[two diversity rules that cost no recall](../../docs/decisions/2026-08-04-two-diversity-rules-that-cost-no-recall.md),
[the fifth tool returned assembled prose](../../docs/decisions/2026-07-29-the-fifth-tool-returned-assembled-prose.md).
