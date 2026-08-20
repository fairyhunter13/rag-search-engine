---
type: Defect
resource: src/coderag/config.py
title: The chunk budget and the token window do not fit each other
description: 2,000 non-whitespace chars produces chunks of p50 904 and p95 1373 tokens against a 768-token window, so 70% of chunks are cut — and two arms then showed the cut costs no recall.
tags: [chunking, embedding, recall]
status: deprecated
generated: { by: claude/opus-5, at: 2026-08-19T16:45:00Z }
---

# The two numbers, each defensible alone

`CHUNK_CHARS = 2000` non-whitespace comes from cAST's unit ([arXiv:2506.15655](https://arxiv.org/abs/2506.15655))
and sits inside the range [864 controlled configurations](https://arxiv.org/abs/2605.04763) explored.
That study finds chunk size has "a weaker, non-monotonic effect" and names no default, so 2,000 is a
round number in a flat region rather than a measured optimum. `EMBED_MAX_TOKENS = 768` *is* a peak:
the 512→768 work, where 1024 regressed.

Neither was chosen against the other, and on real code they do not fit. Tokenising the chunker's
own output with truncation disabled:

| corpus | chunks | p50 | p95 | max | over 768 | tokens dropped |
|---|---|---|---|---|---|---|
| corpus A | 2,157 | 904 | 1,373 | 1,483 | **70.1%** | **27.2%** |
| corpus B | 4,175 | 881 | 1,154 | 1,416 | **62.8%** | **17.5%** |

`_tokenizer` sets `enable_truncation(max_length=EMBED_MAX_TOKENS)`, so the tail is discarded
silently. The lexical lane still indexes the whole chunk; only the dense lane is blind to it, which
is why this reads as a weak semantic lane rather than as a bug.

# Why it stayed invisible

Every chunk arriving at exactly the ceiling is what makes the mean token length *equal* the maximum
— the signature that found this. It also means padding waste is 0%, which is why a batching
experiment aimed at launch overhead came back flat and pointed here instead.

# The verdict: it costs no recall, and both constants stay

Corpus B, 300 queries, semantic lane, one run — the only way these are comparable:

| arm | recall@1 | recall@10 | MRR |
|---|---|---|---|
| `gte-modernbert` (768 / 2000) | 0.1600 | 0.3200 | 0.2079 |
| `gte-win1536` (window past p95) | **0.1600** | **0.3200** | 0.2112 |
| `gte-chunk1000` (chunk cut to fit) | 0.1433 | 0.3133 | 0.1930 |

Attacked from both ends and neither end moves the number the right way. Widening the window past
the p95 changes recall by **exactly nothing** — identical to four decimals at both k — for double
the tokens per chunk. Cutting the chunk to fit the window is **worse** at both k, which is the
sharper half: the 2,000-char chunk is not merely tolerable, it beats the one that fits.

So the 70% figure is real and the inference drawn from it was wrong. The discarded tail is the part
of a 2,000-char chunk that carries no retrievable signal, and the lexical lane indexes it anyway.
The product of the two constants is now measured, and `gte-win1536` is on record as a cost with no
benefit — the more useful half of the finding, because it is the change someone will propose next.

The freeze this file placed on both constants is lifted. They do not move.
