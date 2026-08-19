---
type: Decision
resource: src/coderag/index.py
title: The thermal pause is not what indexing costs, and the profile says so
description: A 25-second py-spy sample of a live indexing worker put 80.7% of self-time inside the ONNX forward pass and 0.04% inside cool_down, so the cooldown stays and the lever is batch size.
tags: [indexing, performance, gpu]
status: stable
generated: { by: claude/opus-5, at: 2026-08-19T16:20:00Z }
---

# The hypothesis, and what the sample did to it

A 7,000-file project takes ~28 minutes, and the obvious suspect was
`CODERAG_INDEX_TEMP_C=84` parking the worker between flushes. `py-spy record -d 25 -f raw
--nonblocking` against the live worker, 2,359 samples:

| frame | self-time |
|---|---|
| `run` (onnxruntime_inference_collection.py:326) | **1,903 = 80.7%** |
| `nonwhitespace` (chunk.py:79) | 6.5% |
| `upsert_file` (store.py:248) | 2.2% |
| `_forward` tokenizer (embed.py:164) | 1.8% |

By containing frame: the embed call 84.2%, `chunk_text` 8.4%, `_flush` 7.3%, and **`cool_down`
one sample — 0.04%**. Removing the thermal governor buys nothing measurable, and it is the only
thing standing between an 87 °C card and an unbounded build. It stays.

# Where the time actually is

`_write_files` calls `embed()` **once per file**. The store holds 7,200 files across 54,937
chunks — **7.63 chunks per call** against an `adaptive_batch` ceiling of 128. Every call pays a
full kernel launch and a padded batch for a fraction of the work it could carry, which is why
`session.run` dominates without the GPU being saturated.

The fix is to accumulate chunks across files up to the batch ceiling rather than flushing the
embedder at each file boundary. Length-bucketing within a batch cuts the padding on top of that,
and the ~19% spent in `chunk_text` and `_flush` is CPU that could overlap the forward pass.

# Why it was not fixed when it was found

The arms of a bake-off are separate subprocesses launched from **the same source tree**. Changing
the indexing hot path mid-run makes arms 4–7 incomparable to arms 1–3 — the run measures the edit,
not the models. The progress counter landed anyway because it is control-flow-identical (counters
only); batching is not, and waits for the run to end.

# The measurement trap this walked into first

`env | grep CODERAG` in an interactive shell showed nothing, which looked like proof that the
thermal gate was not even enabled. It was: `/proc/<worker-pid>/environ` carried
`CODERAG_INDEX_TEMP_C=84`. The daemon's environment is not the shell's, and only one of them is
running the code.
