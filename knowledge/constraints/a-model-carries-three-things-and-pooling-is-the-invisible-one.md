---
type: Constraint
resource: src/coderag/embed.py, src/coderag/config.py, tests/test_pooling.py
title: A model carries three things, and pooling is the one that fails invisibly
description: Prefixes, context limit and pooling all travel with an embedding model. Only pooling produces a plausible unit vector when it is wrong. So a mispooled arm reads as the model losing rather than as a bug.
tags: [embedding, evaluation, models, bake-off]
status: stable
generated: { by: claude/opus-5, at: 2026-08-19T14:10:00Z }
---

# Constraint

Three properties travel with an embedding model and none of them are this pipeline's to choose:

* **prefixes** — `search_document: ` / `search_query: ` for nomic, an instruction sentence for bge,
  none at all for gte-modernbert or jina-code
* **context limit** — 512 position embeddings for bge against nomic's 768-token budget
* **pooling** — CLS for `gte-modernbert-base`, `bge-base-en-v1.5` and `bge-small-en-v1.5`. Masked
  mean for `nomic-embed-text-v1.5` and `jina-embeddings-v2-base-code`

The first two announce themselves. A borrowed prefix scored gte at **0.029**, and bge against
nomic's token budget **failed to load** — both loud. Pooling is silent. Every pooling of a real
hidden state produces a plausible unit vector.

Cosine works, the arm completes, and the number is simply low. Three arms of the embedder bake-off
were scored on a masked mean of a CLS-trained model before anyone noticed. The honest reading of
those numbers is *void*, not *lost*.

The source of truth is the model's own `1_Pooling/config.json`, which is one HTTP request and is
never what a leaderboard summary reports. This was found by checking a model card against its
config while answering an unrelated question about model families. Not by a test, because no test
this repo could write knows what pooling an arbitrary new model expects.

# What this constrains

`EMBED_POOLING` is per-arm config, and it sits **before** the Matryoshka truncation in `_pool()` so
the renormalise covers both branches. A copy of the truncation inside the mean branch would leave
CLS vectors un-normalised after the cut. That is the same silent failure one layer down.

Adding an arm means reading three files from the model repo, not one: `1_Pooling/config.json` for
pooling, `config.json` for `max_position_embeddings`, and the card for prefixes. An arm added
without all three measures the mismatch and reports it as the model's score.
