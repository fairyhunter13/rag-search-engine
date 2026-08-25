---
type: Decision
resource: src/coderag/index.py
title: The thermal pause is not what indexing costs, and the profile says so
description: A 25-second py-spy sample of a live indexing worker put 80.7% of self-time inside the ONNX forward pass, and 0.04% inside cool_down. So the cooldown stays, and the lever is batch size.
tags: [indexing, performance, gpu]
status: deprecated
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

**Deprecated 2026-08-21**. It went. The profile above is not what reversed it — the number still
holds, and that is the point. Costing nothing is also *saving* nothing. What `cool_down` did cost
was a config knob, a `/healthz` field, a systemd `Environment=` line, a test module and this
concept. Deleted for size, not for speed.

`elapsed_s` is uncorrected wall clock now and `cooled_s` is gone from the progress payload.
Throttling is the driver's problem. `free_vram_bytes()` and `adaptive_batch()` stayed — VRAM path,
not thermal. The batch-size section below is untouched by any of this and is still the live
result.

# Where the time is not: batch size

`_write_files` called `embed()` **once per file**: 7.63 chunks against an `adaptive_batch` ceiling
of 128. So the obvious reading of that profile was that the card paid a full kernel launch for 6%
of a batch. It was wrong, and the A/B says so. Moving the embed call into `_flush`, batching 64
files at a time, ABBA on the same repo with a warm model:

| arm | seconds |
|---|---|
| batched flush | 32.7 · 33.0 |
| per file (shipped) | 32.0 · 32.0 |

**2–3% slower.** The change was reverted. Vectors were equivalent (min cosine 0.9999997), so this
is a throughput result, not a correctness one.

The reason is in the token lengths: **every chunk arrives at exactly `EMBED_MAX_TOKENS`**. Padding
waste at batch 128 is 0.0%, sorted or unsorted, because there is nothing to pad — sequences are
already full width. The GPU was doing dense work at 6 items per call and dense work at 128 per
call. There was no launch overhead to recover. Length-bucketing is refuted by the same number.

That is what "80.7% in `session.run`" actually meant. The indexer is **token-throughput bound**,
and the only levers left are fewer tokens or a smaller model — not a better batch shape.

# The measurement trap this walked into first

`env | grep CODERAG` in an interactive shell showed nothing, which looked like proof that the
thermal gate was not even enabled. It was: `/proc/<worker-pid>/environ` carried
`CODERAG_INDEX_TEMP_C=84`. The daemon's environment is not the shell's, and only one of them is
running the code.

The second trap is the one above: a profile localises time, it does not name a cause. "80.7% in the
forward pass" was read as "the batches are too small" and the A/B refuted it in eight minutes.
