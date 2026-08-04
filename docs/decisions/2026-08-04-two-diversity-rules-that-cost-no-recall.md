# Two diversity rules in the result path, and what each one is worth

**2026-08-04.** `query/search.py` had no dedup anywhere, while `chunker` emits windows with a
10-line overlap — so adjacent chunks of one file are near-duplicates *by construction*, and a
top-10 could be most of one file. `_diversify` now runs between the rerank sort and the `top_k`
cut. This records the measurement it shipped on, because at the time there was **no published
evidence for MMR or per-file caps in code retrieval in either direction**, so a local number was
the only thing that could decide it.

> **Amended 2026-08-04.** That last claim is no longer true, in this item's favour. `arXiv:2601.23254`
> reports 7.04–15.58% relative exact-match over its strongest baseline from identifier-weighted
> reranking plus **structure-aware deduplication** — the same move, arrived at independently and
> published before this shipped. It was still right to ship on the local number; the evidence was
> found afterwards, and had it pointed the other way the number would still have decided.
> See `2026-08-04-the-three-questions-the-search-budget-had-left-open.md`.

## Where it runs, and why that position is the only correct one

After the cross-encoder, before `[:top_k]`. The reranker still scores every candidate in the pool,
so nothing is hidden from it; only the answer the caller sees is thinned. Filtering earlier would
starve the reranker of candidates, which is the most expensive thing this module can do.

Both rules keep the highest-reranked member of whatever they collapse, so the result can only be
reordered *within* what the reranker already preferred — a chunk never overtakes a better-scoring
one from a different file. And when the pool is thin enough that both rules would return fewer than
`top_k`, the dropped chunks are backfilled in rerank order: **diversity is a preference between
equally good answers, never a reason to return fewer of them.**

## The gate, and it was label-free first

Distinct files in the returned top-10, both arms computed from **one** retrieval + rerank pass —
arm A is the reranked head, arm B is `_diversify` over the same pool — so the comparison is exact
and no arm gets a different pool by accident. 60 commit-derived queries (`--queries-from commit`,
the population the corpus cannot leak the answer to) per store.

| store | distinct files A → B | recall@10 | recall@1 | MRR@10 | paired rank: better / worse |
|---|---|---|---|---|---|
| this repo | 5.67 → **7.37** | 40 → 40 | 29 → 29 | 0.542 → 0.546 | 3 / **0** |
| a second, larger store | 6.10 → **7.68** | 48 → **49** | 35 → 35 | 0.654 → 0.658 | 4 / **0** |

**+26–30% distinct files, and accuracy does not move down anywhere.** Across 120 queries there are
seven where diversity improves the rank of a gold file and **zero** where it hurts one — which is
what the backfill predicts: a gold file previously buried under a duplicate window is now inside
the cut.

## The ablation, which is why both rules ship

The per-file cap was the weaker claim going in — overlap collapse removes text that is literally
duplicated, whereas a cap only asserts a preference. So each rule was run alone:

| store | baseline | overlap only | cap only | both |
|---|---:|---:|---:|---:|
| this repo | 5.67 | 6.95 | 6.60 | **7.37** |
| second store | 6.10 | 7.00 | 7.20 | **7.68** |

Neither rule subsumes the other: each alone recovers roughly two thirds of the gain, and they are
additive. recall@10 is flat under every arm. The cap earns its lines rather than riding along on
the rule that was easy to justify.

## One limit found while measuring, worth knowing before reaching for commit queries

Commit-derived queries need a repository with real history. Four of the largest stores in the fleet
are single-commit imports — `git log` returns one squashed commit — so the builder correctly
produces **zero** queries there and the mode is simply inapplicable, not broken. Check the commit
count before choosing a store for a commit-mode arm.

## What is not claimed

Two stores and 120 queries. The distinct-files number is large and consistent; the rank numbers are
a *safety* check that passed, not evidence of a retrieval win, and should not be quoted as one. No
stamp and no lease were involved — this is the query path, it changes no stored edge, and a
re-derive would not have been an appropriate response to it.
