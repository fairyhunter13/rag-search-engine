---
type: Defect
resource: src/coderag/config.py
title: The chunk budget and the token window do not fit each other
description: 2,000 non-whitespace chars produces chunks of p50 904 and p95 1373 tokens against a 768-token window, so 70% of chunks are cut and 27% of all tokens never reach the embedder.
tags: [chunking, embedding, recall]
status: open
generated: { by: claude/opus-5, at: 2026-08-19T16:45:00Z }
---

# The two numbers, each defensible alone

`CHUNK_CHARS = 2000` non-whitespace is measured — arXiv:2605.04763 calls it "a robust default"
across 864 configurations. `EMBED_MAX_TOKENS = 768` is also measured: it is the recall peak from
the 512→768 work, where 1024 regressed.

Neither was chosen against the other, and on real code they do not fit. Tokenising the chunker's
own output with truncation disabled:

| corpus | chunks | p50 | p95 | max | over 768 | tokens dropped |
|---|---|---|---|---|---|---|
| `octg_component` | 2,157 | 904 | 1,373 | 1,483 | **70.1%** | **27.2%** |
| corpus B | 4,175 | 881 | 1,154 | 1,416 | **62.8%** | **17.5%** |

`_tokenizer` sets `enable_truncation(max_length=EMBED_MAX_TOKENS)`, so the tail is discarded
silently. The lexical lane still indexes the whole chunk; only the dense lane is blind to it, which
is why this reads as a weak semantic lane rather than as a bug.

# Why it stayed invisible

Every chunk arriving at exactly the ceiling is what makes the mean token length *equal* the maximum
— the signature that found this. It also means padding waste is 0%, which is why a batching
experiment aimed at launch overhead came back flat and pointed here instead.

# What is not yet known

Whether it costs recall. The 512-token `bge-base` arm — which drops strictly more — won recall@10
in the seven-arm run (0.3433 against `gte-modernbert` 0.3200 at 768), so the dropped tail may carry
little. Two arms are running against corpus B to settle it from both ends: `gte-win1536`
widens the window past p95, `gte-chunk1000` cuts the chunk to fit the window. Read them within the
run against `gte-modernbert`, never against a level from another run.

Do not change either constant until those land. Both numbers have evidence behind them; only their
*product* is unmeasured.
