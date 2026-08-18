---
type: Component
resource: src/rag_search/query/
title: "query: answering a question"
description: The read path — two sqlite lanes fused on rank, fanned out across federation members by eight threads that must never touch the GPU, and a scope vocabulary that must not advertise what it does not do.
tags: [query, search, rrf, fanout, graph-handler, scopes]
status: active
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# query: answering a question

Four files, ~775 lines. `search.py` retrieves, `graph_handler.py` answers relation queries,
`ask.py` assembles an architecture view, `answer_cache.py` is a TTL file cache. The two-stage shape
is [retrieval is recall then rerank](../constraints/retrieval-is-recall-then-rerank.md). Both GPU
touches go through [embed](embed.md); relation answers read the store built by
[graph](graph.md).

## Eight fan-out threads, and why threads are safe here

`_FANOUT_WORKERS = 8` runs members concurrently because both lanes are pure sqlite — vec0 KNN and
FTS5 — and release the GIL. The GPU is touched **once before the loop and once after**, never
inside it, so the fan-out never contends with `_GPU_INFER_LOCK`. Moving an embed or a rerank inside
the loop turns eight parallel readers into eight threads queued on one lock.

Each member holds its own connection opened `check_same_thread=False`, and no two workers ever touch
the same one. The union itself is
[a federation is a query-time union](../constraints/a-federation-is-a-query-time-union.md).

Sizing evidence: the largest workspace's 157 priced members cost 36.68 s of sequential dense KNN
(mean 233.6 ms, worst 3.28 s) against 3.00 s of lexical.

## Fusion is on rank, never on score

Cosine similarity and BM25 share no scale, no range and opposite polarity, so no weighting of the
two numbers means anything. RRF fuses positions instead, with `k = 60`. A chunk found by one lane
only still scores — one term instead of two — which is how a rare identifier no embedder placed near
the query enters the pool.

## A scope name that changes nothing is a lie

`ask.py` contains no LLM. Its `_SCOPES` only selects which community map is assembled; every scope
runs the identical federated search underneath. Adding a name to that tuple without changing the
assembly advertises a capability that does not exist, and the caller has no way to tell.

`graph_handler._RELATIONS` is the same hazard on the other axis: it must stay word-for-word in sync
with the MCP and CLI docstrings, and `test_surface_consistency.py` is what holds it there.

## Guards

| Claim | Guard | File |
|---|---|---|
| every advertised relation is implemented | `test_sc4_every_advertised_relation_is_implemented` | `test_surface_consistency.py` |
| every advertised scope is a distinct assembly | `test_sc5_every_advertised_scope_is_a_distinct_assembly` | `test_surface_consistency.py` |
| fan-out preserves store order | `test_t4_fanout_preserves_store_order` | `test_query.py` |
| a lexical-only hit still surfaces | `test_tk4_lexical_lane_finds_what_dense_misses` | `test_hybrid_retrieval.py` |
