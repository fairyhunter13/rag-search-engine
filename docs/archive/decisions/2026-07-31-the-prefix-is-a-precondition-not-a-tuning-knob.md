# The challenger's margin was the prefixes, and most of its first headline was the query set

**2026-07-31** · P5 · `scripts/eval_retrieval.py`, `embed/embedder.py`

[The retrieval-eval harness](2026-07-31-retrieval-eval-harness.md) measured nomic beating the
incumbent on all four metrics and correctly declined to switch, naming three things standing in the
way: the query set was Python-only, the challenger ran without its task prefixes, and 512-position
models could not be run at all. All three are now closed. The answer to each changed what the first
result meant.

Ten arms, two projects, dense lane. **Only same-`max_tokens` rows are comparable** —
`RSE_EMBED_MAX_TOKENS` also drives `index/chunker.py`, so a 512 arm gets a differently chunked
store.

**This repo — 224 files, 130 queries (python 118, go 11, html 1):**

| arm | chunks | recall@1 | MRR | nDCG@10 | recall@10 |
|---|---:|---:|---:|---:|---:|
| jina-v2-base-code (incumbent) | 1,316 | 0.6077 | 0.6963 | 0.7381 | 0.8692 |
| nomic-v1.5, no prefixes | 1,316 | 0.6154 | 0.7036 | 0.7404 | 0.8538 |
| **nomic-v1.5 + prefixes** | 1,316 | **0.6692** | **0.7432** | **0.7759** | **0.8769** |
| jina, 512 (control) | 2,118 | 0.7385 | 0.7883 | 0.8135 | 0.8923 |
| **bge-base-en-v1.5, 512** | 2,118 | **0.7846** | **0.8457** | **0.8721** | **0.9538** |

**A 2,464-file php+javascript+vue project — 200 queries (php 61, python 61, vue 32, javascript 30,
scss 10, bash 4, sql 1, typescript 1):**

| arm | chunks | recall@1 | MRR | nDCG@10 | recall@10 |
|---|---:|---:|---:|---:|---:|
| jina-v2-base-code (incumbent) | 21,194 | 0.1900 | 0.2766 | 0.3283 | 0.4950 |
| **nomic-v1.5 + prefixes** | 21,194 | **0.2650** | **0.3510** | **0.3984** | **0.5500** |
| jina, 512 (control) | 31,428 | 0.2350 | 0.3062 | 0.3518 | 0.5000 |
| bge-base-en-v1.5, 512 | 31,428 | 0.2350 | 0.3352 | 0.3920 | **0.5750** |

## The prefix is a precondition, not a tuning knob

FastEmbed does not supply task prefixes. `TextEmbedding.query_embed` and `passage_embed` delegate to
the model class, and the base `OnnxTextEmbedding` implementations of both fall straight through to
`embed()` unchanged; in 0.8.0 the only prefix-aware class in the package is `multitask_embedding.py`,
which nomic does not use. `Embedder.embed` then passes its texts to the session verbatim. Nothing
anywhere in this stack was adding them.

Unprefixed, nomic beats the incumbent by **+0.008 recall@1** — a tie. Prefixed, by **+0.062**. The
entire margin is the two strings. So a switch that changed `EMBED_MODEL` and nothing else would have
bought approximately nothing, at the cost of re-embedding every vector in the fleet.

## Most of the original headline was the selector, not the model

The first result read +0.115 recall@1 for nomic. Under the symbol-derived, language-stratified query
set it is **+0.062** on the same repo — a bit over half. The rest was the docstring selector: a
Python docstring is prose describing a function, which is the shape of question a general-purpose
text model is trained on and a code-specialised model is not. The harness was not measuring which
model retrieves code better; it was partly measuring which model likes English.

This is the same lesson as the saturating hybrid lane, one level in: **an A/B inherits every bias in
how its questions were built**, and derived ground truth is not automatically neutral ground truth.

## The challenger holds, and holds harder where it matters

Prefixed nomic against the incumbent at identical chunking, all four metrics, both projects:

|  | recall@1 | MRR | nDCG@10 | recall@10 |
|---|---:|---:|---:|---:|
| this repo (91% python) | +0.062 | +0.047 | +0.038 | +0.008 |
| php/js/vue project | +0.075 | +0.074 | +0.070 | +0.055 |

Eight of eight positive, and **wider on the multi-language project** — +39% relative on recall@1
against +10% here. The code-specialised incumbent loses by more, not less, once the questions stop
being Python.

Absolute levels collapse between the two projects (0.61 → 0.19 for the incumbent) because the store
is sixteen times larger and every query has that many more distractors. Read the deltas.

## The 512-position models: one correction, one still broken

`bge-base-en-v1.5` runs and is the strongest arm on this repo, winning all four against its
same-chunking control. On the larger project it ties on recall@1 and wins the three deeper metrics,
so its advantage is real but thinner than one project suggested.

`gte-base` still fails, and **not for the reason previously recorded**. The earlier note attributed
both failures to `enable_truncation(EMBED_MAX_TOKENS)` forcing 768 onto a 512-position model. That
was true of bge, which now runs at 512. gte fails at 512 too, inside its tokenizer —
`setting an array element with a sequence … inhomogeneous shape (32,)` — ragged output, a different
defect. One cause was assumed to cover two symptoms.

## `EMBED_MAX_TOKENS` is a lever, and it shrinks with scale

The same model at 512 gains **+0.131 recall@1** here and **+0.045** on the larger project. Real in
both, but the effect that looked dominant on a 1,316-chunk store is a third of that on a 21,194-chunk
one. `core/config.py` justifies 768 with a four-point sweep quoted to four decimals whose query set
does not exist; this is the first evidence against that number, and it is not yet enough to move it.

## Decision: the evidence licenses a switch, and the switch is bigger than it looked

`AUTO_MIGRATE_VECTORS` stays `0`. Nothing changes in this commit. What the arms establish is that
the incumbent is beaten consistently, and what they also establish is that acting on it costs more
than swapping a string:

- **Production has no asymmetric embed path.** `Embedder.embed` is called by both the indexer and
  the query path, and neither tells it which side it is on. The prefixes are the whole margin, so
  the switch requires that distinction to exist first — an API change reaching `index/indexer.py`
  and `query/search.py`, not a config edit.
- **It is a full re-embed, not a re-derive.** The `e7`+`fg3` stamp move re-derived graphs from text
  already on disk in 21.6 minutes. Changing `EMBED_MODEL` invalidates every vector in 150 stores and
  every one has to go back through the GPU.
- **`index/chunker.py:27` must move in the same commit.** `Tokenizer.from_pretrained(EMBED_MODEL)`
  is where a family change actually pays, and leaving it is how `EMBED_MAX_TOKENS=768` comes to mean
  two different budgets.

## The cell nobody measured is the one a switch would land in

The grid is model × `max_tokens`, and **nomic + prefixes at 512 was never run** — yet that is the
configuration any switch would ship, since both levers point the same way. The two levers are not
additive by assumption: finer chunks and a prefix that says "this is a document" may well overlap.
Measure that cell before scheduling the re-embed, not after.
