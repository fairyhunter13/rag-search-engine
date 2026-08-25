---
type: Defect
resource: src/coderag/rank.py, tests/test_search.py
title: The pool cut starved the members it was built to reach
description: "pool_cut gave the caller's own project half of 60 slots and round-robined the members through the other 30. The fix that widens a member cwd to its root's whole unit raised the member count from 135 to 337. So 307 projects never reached the reranker, and the leaf holding the answer was one of them."
tags: [search, federation, ranking, resolved]
status: stable
generated: { by: claude/opus-5, at: 2026-08-25T23:55:00Z }
---

# The earlier fix moved the input, not the cut

[The pre-rerank cut ranked 136 projects by scores that are only meaningful within
one](per-project-ranks-were-truncated-as-if-comparable.md) built `pool_cut` for a root that
federated 135 members. `limit` was `CANDIDATES`, which is 60. The caller's own project took
`limit // 2` slots and the members shared the rest.

Then `federation.unit` widened a member cwd to its root's whole unit. A search from one redacted-name-7
leaf now spans 358 projects rather than 1. The quota did not move with it. 337 members shared 30
slots, in the order the federation lists them, and 307 of them reached nothing.

# Measured on the live corpus

One query, replayed offline against the real stores from a member cwd:

```
unit size: 358 | raw pool: 14720 from 338 projects
own member hits: 60 | target-leaf hits: 60
after pool_cut: 60 | own: 30 | target survives: False
distinct projects in cut: 31
```

The leaf that answers the question produced 60 candidates. None survived the cut. Recall was never
the fault. A live run proved that separately. The same query with `path_glob="Services/*.php"`
returns the answering file at rank 3. That works because `_filter` runs before `pool_cut`, so the
scope removes the caller's own noise before the quota applies.

# `limit` is a floor now

The round-robin has to reach the last member, so the pool grows to
`own_share + len(members)` where that exceeds `limit`. `own_share` is still computed from the
original `limit`, so the caller's own half does not inflate with the member count. Every project
that produced a candidate contributes its best hit.

```
pool_cut size: 367 | own: 30
distinct projects: 338 of 338 that had hits
target leaf survives: True
```

# What the extra slots cost

Measured with the daemon stopped, so the reranker had the GPU alone. Retrieval over 358 projects
takes 3.34 s and does not move. The rerank of 60 chunks takes 590 ms, and of 367 chunks 3792 ms.
That is 9.8 ms and 10.3 ms per chunk, so the cost is linear and the batch size holds.

So a federated search pays about 3.2 s more. It buys an answer that was absent before. The answer
lands at rank 8 of 10 on the query that returned nothing.

# Why the pool grows rather than the members getting ordered

Ordering the members by their fused score is the option this rejects, for the reason the earlier
defect records. RRF is a rank within one project. Every project's rank-1 hit scores about the
same, so a flat sort across projects orders by nothing. The cross-encoder is the only stage that
compares across projects, and reaching it is the whole point of the cut.

# Two ranking effects this does not close

Both are recorded in the workspace bundle rather than here, because neither lives in the engine.
A generated JSON digest still takes rank 1 on a domain-code question. Consecutive pins of one
domain repo still spend several ranks on near-identical copies of one file. `diversify`
fingerprints on the leading 400 characters, and those characters differ between the pins.
