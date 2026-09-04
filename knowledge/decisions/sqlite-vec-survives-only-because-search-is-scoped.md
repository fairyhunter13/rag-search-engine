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

# 2026-09-03: the criterion was met 120 times over, and the answer was not an ANN index

The reversal condition above is a **scoped p95 over ~200 ms**. Measured from `searches.jsonl` on
2026-09-03, over 7,801 rows:

| search unit | n | p50 |
|---|---|---|
| 1 to 5 projects | 6,526 | 179 ms |
| over 100 projects | 559 | **24,631 ms** |

The p50 of the second row meets a p95 criterion of 200 ms by a factor of 120. The largest root on
this fleet holds 361 projects in its unit, and one of its searches ran 478.7 s.

The answer taken is a repack and a thread pool, and not an ANN index. Three reasons, in the order
they were checked.

**The scope was never the thing that grew.** The criterion assumes a scan over one project's
vectors. That scan is still under 10 ms. What grew is the number of projects the scan is run
against, one at a time, in a plain `for` loop. An ANN index makes a 10 ms scan faster and leaves
361 sequential round trips exactly where they are.

**Most of what the scan read was dead.** `chunks_vec` never gave a block back, so a KNN read 6.3x
the live bytes on the busiest store —
[a vector table kept every block it ever allocated](../defects/a-vector-table-kept-every-block-it-ever-allocated.md).
The repack cut the warm KNN from 300 ms to 69 ms with no change to the algorithm. An ANN index
built over the same bloat would have inherited it.

**The loop is disk-bound and `sqlite3` releases the GIL for a read.** So the cheapest correct fix
is `config.FANOUT_WORKERS` threads over the same code — `conns.fanout`, called once at
`search.py:242`.

The criterion is not withdrawn, and it is not met either. It is **still unmeasured**, because no
number here isolates the per-project scan: the two figures above are unit totals over a throttled
card, and the 200 ms it names is a scan. What reverses this decision is a single-project p95 over
200 ms, measured after the repack, on a host that is not three degrees past its throttle point.

# 2026-09-04: the single-project scan was measured, and three ways to shrink it were refused

The paragraph above rejected quantization on arithmetic. It is now rejected on measurement, and the
arithmetic it used was wrong: the fleet holds **465 stores, 400,600 vectors, 1.23 GB** of float32
payload, not the 1.8 GB stated at the top of this card.

Measured warm on the largest store on the fleet, 41,636 vectors and 154 MB of vec0 blocks after the
repack, 30 queries per arm. Warm and not cold, because swap was full at a load average of 18.91 and
a cold-cache read on this host measures the host —
[this host cannot produce an admissible latency number](../constraints/this-host-cannot-produce-an-admissible-latency-number.md).

| arm | p50 | p95 | blocks |
|---|---|---|---|
| float32, `k=60` | 76.7 ms | 79.4 ms | 154 MB |
| int8 + float32 rescore, `k=240` | 77.1 ms | 77.9 ms | 32 MB |

**One fifth of the bytes and the same latency.** At 125 MB the working set is far past L3, so the
scan is bound by memory bandwidth and float32 already saturates it. Narrower elements only pay
where the arithmetic dominates, which is the opposite of this shape.

Accuracy would have passed: the rescored top-10 matched float32 on **30 of 30** queries at
oversample 1. So this is refused on cost, not on correctness. The cost is a second table that must
stay coherent with the first, `+0.31 GB` over the fleet, and a rewrite of `vector_blocks`,
`vector_waste` and `repack_vectors`, each of which names one table today. `store.py` sits at 215
executable lines against the 220 ceiling, so the vector plane would have to move to its own module
before any of that could be written.

Two nearby levers were measured rather than argued, so neither has to be re-derived.

**Binary quantization is fast and wrong here.** 12.6 ms p50 against 72.5 ms, and 4.0 MB of blocks
against 154 MB. It is the only variant that moved the warm number, because popcount replaces the
per-dimension arithmetic. Rescored recall@10 was **0.14 at oversample 4 and 0.41 at oversample 8**.

**Dimension truncation fails on this model.** `EMBED_TRUNCATE_DIMS` already exists, so the arm cost
nothing:

| dims | blocks | p50 | recall@10 |
|---|---|---|---|
| 768 | 129 MB | 73.3 ms | 1.000 |
| 512 | 86 MB | 47.4 ms | 0.747 |
| 384 | 64 MB | 38.0 ms | 0.500 |
| 256 | 43 MB | 24.9 ms | 0.267 |

Truncation is a summary only for a Matryoshka-trained model, whose dimensions are ordered by
importance. `gte-modernbert-base` is not one, so cutting its tail cuts signal at random.

An ANN index stays refused for the reason above it: 400,600 vectors over 465 scopes is two orders
of magnitude below where DiskANN or IVF starts to pay, whatever `sqlite-vec` 0.1.10 adds.

The reversal condition is unchanged and is now **partly measured**: the warm single-project p95 is
79.4 ms against the 200 ms it names. What is still missing is the same figure on a host that is not
throttled and not swapping.
