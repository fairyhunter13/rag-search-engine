# Personalized PageRank over the call graph: built, measured, reverted

**2026-08-03** · P10 · nothing shipped — `query/graph_rank.py`, a `VectorStore.db_path` property,
a `graph_db` argument to `query/search.py:_rank`, and two live gates existed for the length of one
A/B and were deleted.

The last item on the lexical/ranking track was Aider's design: a graph over *files*, an edge per
resolved call, **the query as the personalization vector**, and the resulting per-file PageRank
folded into the ranking. Its stated justification was that
`grep query/search.py` for `graph|edges|community_id` returns nothing — the call graph feeds
`graph()` and `overview(what="communities")` and contributes **zero** to `search`. Every point of
call-resolution recall the extractor ladder has ever bought is navigation, not retrieval.

It was built, measured on the corpus it is for, and reverted. **The graph is a worse ranker than
the pipeline it was going to advise.**

## The measurement

`scripts/eval_retrieval.py --lane hybrid --queries 300` against a real php/js/vue/python store,
arms differing only in `_GRAPH_WEIGHT` (0.0 vs 0.25):

| | PPR off | PPR on |
|---|---:|---:|
| recall@1 | **0.5267** | 0.5167 |
| MRR | **0.6359** | 0.6227 |
| recall@10 | 0.8767 | 0.8767 |

Paired McNemar on recall@1 over 268 shared queries: **13-10 for the off arm, p = 0.68**. Every
aggregate is neutral-to-worse and nothing is significant in either direction.

**This is not the null a mis-matched instrument produces.** That distinction is the whole reason
the two records before this one exist — [the tokenizer](2026-08-03-the-eval-harness-cannot-see-a-tokenizer.md)
and [the path field](2026-08-03-a-path-is-a-field-not-a-line-of-body.md) both read null here
because the harness structurally could not ask their question. So it was checked rather than
assumed: **a seed fired on 295 of the 300 queries**, and there were **23 discordant pairs** against
the path field's 1. The term ran, it moved results, and the results it moved were a wash.

## Why, in one number

The harness builds each query as `qualified_name` + the definition's first line, so the query names
an indexed symbol almost by construction — the single most favourable population this signal can
have. On that population, **PPR's own top-ranked file is the gold file 76 times in 300 (25.3%),
against the existing pipeline's 52.7%.**

So the graph's ranking is roughly half as good as the ranking it was being mixed into. An RRF term
can only help if it is both decorrelated *and* competitive; this one is neither, and lowering the
weight only asymptotes back to the baseline. There is no positive effect to tune toward.

## What that says about the graph, and what it does not

It does **not** say the graph is wrong. Edge precision is 1.000 by construction (cap-1 emits only
when exactly one candidate survives) and nothing here contradicts that. What it says is narrower
and more useful:

- **A call edge is a weak relevance signal even when it is a correct call edge.** The files a file
  calls are not the files a question about it is answered by, often enough to beat a cross-encoder
  that read the actual text.
- **52.9% of fleet symbols live in languages with zero edges**, so for over half the index this
  mechanism returns `{}` by construction and could never have contributed anything.
- **Aider ships no measurement.** Its PageRank is prior art, not evidence, and its seed is a
  different thing — the files the user is *editing*, in a tool that has no cross-encoder and no
  dense lane to be better than. Borrowing the design without the context was the risk the plan
  flagged when it wrote that #12 "has to earn its place on a local A/B and nothing else."

So the finding stands as recorded: **the call graph does not currently improve retrieval, and the
reason is not that it is unwired.** Wiring it was one afternoon. Anything that reopens this owes a
mechanism that beats 25.3%, not another way to blend the same 25.3% in.

## What was deleted, and the one thing kept

Deleted: `query/graph_rank.py` (PPR, Aider's three surviving priors, the mtime-keyed graph cache),
`VectorStore.db_path`, `_rank`'s `graph_db` parameter, and gates GR1/GR2. Kept: nothing in `src/`.
The probe arms are in the session scratchpad.

Two design notes worth keeping even though the code is gone, because both would have to be
re-derived by anyone who reopens this:

- **The term has to sit after the cross-encoder, not before it.** `_rank` re-sorts the entire pool
  by rerank score and discards the retrieval order, so a graph boost applied to fusion changes pool
  *membership* only and is invisible in the top 10. A first draft that "worked" on paper would have
  measured nothing at all.
- **An unseeded PPR must return "no opinion", never a uniform vector.** With an all-zero
  personalization vector PageRank degenerates to global popularity, which would reorder every query
  in the fleet on evidence about none of them. GR2 was the gate for exactly that, and it is the
  half of the design that was right.
