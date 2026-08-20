---
type: Decision
resource: src/coderag/config.py, src/coderag/embed.py, tests/eval.py, tests/significance.py, tests/test_embed_gpu.py, tests/test_pooling.py
title: The embedder is settled by a tie-break, because the two finalists are not distinguishable
description: "Ten arms over 300 paired queries: bge-base and gte-modernbert differ by 0.023 recall@10 with p=0.39, so the pre-committed order decides it — fp16 ONNX, then window, then incumbency. The header arm is the one result that is not a tie."
tags: [embedding, bake-off, statistics, chunking]
status: stable
generated: { by: claude/opus-5, at: 2026-08-20T04:00:00Z }
---

# The table

One corpus, 300 CodeSearchNet-protocol queries, dense lane, rerank off, all arms scored on the
**same** queries so every comparison below is paired. Baseline `nomic`, recall@10 0.2867.

| arm | recall@1 | recall@10 | MRR | Δ recall@10 | 95% CI | p (BH) |
|---|---|---|---|---|---|---|
| `bge-base` | 0.1267 | 0.3433 | 0.1950 | +0.0567 | [+0.007, +0.107] | 0.060 |
| `gte-modernbert` | 0.1600 | 0.3200 | 0.2074 | +0.0333 | [−0.007, +0.077] | 0.205 |
| `overlap-300` | 0.0800 | 0.2867 | 0.1454 | 0.0000 | [−0.017, +0.017] | 1.000 |
| `no-header` | 0.0567 | 0.2067 | 0.0998 | **−0.0800** | [−0.117, −0.043] | **<0.001** |

Earlier runs, same protocol, kept for the shape rather than for cross-run comparison: `jina-code`
0.2500 — the code-specialised embedder placed **last**, which is the answer to "why not a model made
for code" and is the normal result on CoIR. `nomic-fp16` scored identically to `nomic`, so fp16 is
free. `gte-win1536` (window past the p95) and `gte-chunk1000` (chunk cut to fit) are the truncation
arms, refuted in their own defect file.

# The decision

**`gte-modernbert-base`, CLS pooling, no prefixes.** Not because it won — it did not.

`bge-base` is ahead of it by 0.0233 recall@10, 28 queries won against 21 lost, **p = 0.39**, a CI
spanning zero. n=300 does not resolve that, and no amount of re-reading four decimals changes it.
The tie-break was written down **before** the numbers arrived, in this order:

1. **an fp16 ONNX export exists** — gte yes, bge fp32-only. Halves the resident model on a card
   already sharing 16 GB with the indexer, at a cost measured at zero.
2. window headroom (8192 vs 512) — carried no recall here, but it is what a larger `CHUNK_CHARS`
   would need later.
3. incumbency.

The first criterion decides it. Stating the order first is what stops the result from being read
backwards into a preference.

`bge-base` remains the runner-up on record, one measurement away from reversing this: a scoped
recall@10 gap that survives a paired test at a larger n. Changing the model re-stamps every store
and forces a full rebuild of 148 projects, which is the reason this is decided before the fleet
index and not after.

# The one arm that is not a tie

**`no-header` loses by 0.0800 recall@10, 5 queries won against 29 lost, p < 0.001 after
Benjamini-Hochberg.** It is the largest effect in the whole bake-off, larger than any difference
between models.

This matters more than the model choice. Before this run, the scope header was the repo's one
**unevidenced** design claim: no published result shows a regex/heuristic header improving
retrieval, every measured gain in that literature comes from an LLM-generated blurb or from
context built into the embedder's training, and the paper this repo cited for it said something
else. The arm was run to close that, with the verdict pre-accepted either way. It came back
strongly for the header.

`overlap-300` is the opposite outcome and equally useful: **exactly zero**, 3 wins and 3 losses.
Chunk overlap buys nothing on this corpus and costs storage, so `CHUNK_OVERLAP` stays where it is.

# How to read a future arm

Aggregates cannot resolve differences this size — 0.02 recall@10 is ~7 queries in 300 — so
`eval.py` emits per-query ranks and `significance.py` reads them paired: exact-binomial McNemar over
the discordant queries, a bootstrap CI that resamples queries rather than runs, and
Benjamini-Hochberg because k arms against one baseline is k tests. The BH column is why `bge-base`
is reported as a tie and not as a winner: its raw p is 0.030 and its corrected p is 0.060.

Named follow-ups, not run: Qwen3-Embedding-0.6B, EmbeddingGemma-300M, granite-embedding-r2. Each is
another arm plus an ONNX-export verification, and each that wins costs a full re-index.

# Wired, 2026-08-20

Recorded first and shipped second, which is the shape this repo keeps finding: `config.py` read
*"Provisional until the bake-off replaces it"* for a day after the decision was `stable`.
`EMBED_MODEL`, `EMBED_POOLING` -> `cls`, `EMBED_ONNX_FILE` -> `onnx/model_fp16.onnx`, and both
prefixes -> `""`.

fp16 was the first tie-break criterion and it was verified rather than assumed, since the free-fp16
measurement was nomic's: the whole `-m gpu` lane, 30 tests, passes on the fp16 export — GPU provider,
768 dims, unit norm, and the natural-language query still ranking its own document first. The fp32
`onnx/model.onnx` is published beside it and `CODERAG_EMBED_ONNX_FILE` is the rollback.

The prefix mechanism stays and only the strings are blank. `side` still raises on an unknown value,
and `test_the_sides_differ_exactly_when_the_prefixes_do` asserts both halves: the two sides agree now
*and* stop agreeing under a prefix, so a prefix silently reintroduced for a model not trained with
one fails there rather than in a recall number six months later.

Every store on disk now reads incompatible on `embed_model` and rebuilds. That is why this landed
before the fleet index and not after.
