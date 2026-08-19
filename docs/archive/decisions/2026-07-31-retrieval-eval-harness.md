# The evidence for `EMBED_MODEL` was not in the repo, and the obvious way to measure it saturates

**2026-07-31** · P5 · `scripts/eval_retrieval.py`

`core/config.py` justifies three of the most consequential settings here — `EMBED_MODEL`,
`RERANK_MODEL`, and `EMBED_MAX_TOKENS` with a four-point sweep quoted to four decimals — against a
"40-query golden set". **That query set is not in the repo.** Grepping the tracked tree for any of
its figures returns nothing. So the numbers can be quoted and cannot be checked, and no challenger
model can be compared against them at all, which is exactly what the open A/B item asked for.

Same failure `measure_preconditions.py` was landed to end, one level up: there the *harness* kept
being rewritten from scratch; here the harness's **inputs** did not outlive the session that took
them. A measurement you cannot re-run is a claim, not evidence.

## The query set has to be derived, or it will be lost again

`scripts/eval_retrieval.py` builds its queries mechanically: each query is a docstring, each
positive is the file carrying it (the CodeSearchNet protocol), selection is deterministic, and one
query per file so a single fat module cannot dominate. Nothing project-specific is committed — the
set is regenerated from whatever `--project` names, which is also why this can live in a public repo
without carrying a real path (P18/HR34).

## The first honest result was that the default measurement is worthless

The full pipeline saturates. Measured on a 1,278-chunk store built from this repo:

| lane | recall@1 | MRR | nDCG@10 | recall@10 |
|---|---:|---:|---:|---:|
| lexical only | 0.925 | 0.9625 | 0.9723 | 1.000 |
| **fused + reranked** | **1.000** | **1.000** | **1.000** | **1.000** |
| dense only | 0.725 | 0.8102 | 0.8503 | 0.975 |

The cause is structural, not a bug: the docstring is still inside the chunk it identifies, so BM25
matches it verbatim. Every arm ties at 1.000 and the A/B answers nothing.

So `--lane dense` is the default. It is the only lane an embedding model controls, which makes it
both the discriminating measurement and the honest one — attributing a fused-and-reranked score to
the embedder was always going to launder the reranker's work into the embedder's column.
`--lane hybrid` measures the pipeline, for a reranker or fusion change, and never for comparing
embedders. **Absolute levels are not comparable to the historical figures**, which came from a
hand-written set this cannot reconstruct; read the delta between two arms.

## The incumbent lost, and that is not enough to switch

Same repo, same 130-query derived set, each arm indexed into its own scratch store with its own
model, dense lane:

| arm | recall@1 | MRR | nDCG@10 | recall@10 |
|---|---:|---:|---:|---:|
| `jina-embeddings-v2-base-code` (incumbent) | 0.7308 | 0.8081 | 0.8436 | 0.9538 |
| `nomic-embed-text-v1.5` | **0.8462** | **0.8905** | **0.9115** | **0.9769** |

Every metric moves the same way, and the direction held at a smaller sample (40 queries: 0.725 →
0.800). Fifteen more queries out of 130 land their gold file first. The code-specialised incumbent
lost to a general-purpose model.

**Superseded by [the prefix result](2026-07-31-the-prefix-is-a-precondition-not-a-tuning-knob.md):**
the +0.115 below is roughly half selector. Under the symbol-derived set the same comparison reads
+0.008 unprefixed and +0.062 prefixed. The conclusion — do not switch on this — was right; the size
of the gap was not.

**No model change is being made on this.** One repo, Python-only queries, docstring-shaped
questions; the fleet is 152 stores across many languages. `AUTO_MIGRATE_VECTORS` is off precisely
because changing `EMBED_MODEL` invalidates every vector everywhere, so the bar is a multi-project,
multi-language run, not this. The challenger was also measured *without* the task prefixes its model
card asks for, so its own number here is a floor. What this establishes is that the incumbent's
margin is not safe and the question is now cheap to re-ask.

## Two candidate arms could not be run at all

`gte-base` and `bge-base-en-v1.5` both fail immediately — one on ragged tokenizer output, the other
inside ONNX Runtime with `right operand cannot broadcast on dim 1 … RightShape: {1,512,768}`.

Both are 512-position models, and `embedder.py` unconditionally forces
`enable_truncation(max_length=EMBED_MAX_TOKENS)` — 768 — onto whatever tokenizer it just loaded.
That backstop exists for a real reason (8192-token sequences make FusedMatMul request 24 GB on a
16 GB card) and it is correct for the model actually configured. It is simply not model-agnostic,
so **the A/B surface silently excludes every 512-position model**. Worth knowing before reading any
future comparison as a survey of the field: it is a survey of the models whose context is at least
`EMBED_MAX_TOKENS`. Nothing was changed to accommodate them — the fix belongs with a decision to
switch, not ahead of one.

## Transferable

- **A retrieval benchmark that scores 1.000 is measuring the leak, not the retriever.** Check for
  headroom before trusting any arm-to-arm delta.
- **Attribute a metric to the component that controls it.** Comparing embedders on a reranked score
  credits the embedder with the reranker's work.
- **Derive ground truth mechanically or it will not survive the session.** Hand-labelled sets get
  quoted long after the file is gone.
