---
type: Decision
resource: src/coderag/store.py, src/coderag/search.py
title: sqlite-vec, with the measurement that reverses it stated up front
description: Brute-force vector scan in SQLite is chosen over an ANN index, because search is scoped to one project plus its members (~4k chunks). The kill criterion is a scoped p95 above ~200 ms.
tags: [storage, vectors, latency, kill-criterion]
status: stable
generated: { by: claude/opus-5, at: 2026-08-19T09:55:00Z }
---

# Decision

One SQLite file per project, holding rows, FTS5 and `sqlite-vec` vectors together. No vector
server, no ANN index, no quantization. 578k × 768 × 4 B ≈ **1.8 GB of vectors across the whole
fleet**, and a per-project scan over ~4k chunks is sub-10 ms.

# The kill criterion, stated because this choice is otherwise indefensible

`sqlite-vec` is **brute force**. At scale it is two to three orders of magnitude behind HNSW. Any
benchmark that puts a million vectors in one table will say so. This choice survives on exactly
one property: **search is scoped to one project plus its members**. So the scan is over thousands
of vectors, not millions.

That makes the reversal condition measurable rather than a matter of taste. **if scoped p95
exceeds ~200 ms, the answer is an ANN index, not a bigger fan-out.** Widening the scope to
compensate makes both numbers worse. Anyone reading a slow query here should check the scope
first. An unscoped query is not a slow version of this design, it is outside it.

**The criterion is currently unmeasurable on this host**, which is a different thing from being
met. The card runs three degrees past its own throttle point at ~15% of rated clock, so a p95
taken here is a cooling measurement. See
[this host cannot produce an admissible latency number](../constraints/this-host-cannot-produce-an-admissible-latency-number.md).
Reversing this decision on a throttled number would buy an ANN index to fix a fan.

Quantization was rejected on the same arithmetic. The old engine carried a binary index and a
hamming coarse pass over an oversampled candidate set feeding an exact float32 rerank. Against
1.8 GB total and a sub-10 ms scan it buys nothing, and it costs a second index that has to stay
coherent with the first.
