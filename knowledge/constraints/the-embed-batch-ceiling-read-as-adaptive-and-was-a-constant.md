---
type: Constraint
resource: src/coderag/gpu.py, src/coderag/embed.py, tests/cpu.py
title: The embed batch ceiling read as adaptive and was a constant, and 32 is where the VRAM stops falling
description: "`adaptive_batch` scales to free VRAM and its adaptive branch never bound on a 16 GB card, so the live batch was always its ceiling of 128. Swept 2026-09-04 over 128/64/32/16: 128 and 64 both hold 4,440 MiB, 32 holds 2,392 and 16 holds 2,398. The floor is the model weights, so the -50% threshold written before the sweep was never reachable and 32 takes the whole win at +2.6% wall."
tags: [vram, gpu, batching, measurement]
status: stable
generated: { by: claude/opus-5, at: 2026-09-04T00:00:00Z }
sources:
  - id: adaptive-batch
    resource: src/coderag/gpu.py
  - id: cpu-harness
    resource: tests/cpu.py
---

# The knob that was not one

`gpu.adaptive_batch` reads as a function of free VRAM. On this card it is a constant:

```
per_item_bytes = EMBED_MAX_TOKENS * 4 * 1024   = 3,145,728
budget         = free_vram // 2                ~ 8,000,000,000
budget // per_item                             ~ 2,580
returned       = min(ceiling=128, 2580)        = 128
```

The adaptive branch binds only below about 786 MiB free. A 16 GB card never reaches that while it
is the thing under measurement, so the ceiling won every call and the live batch was 128 at 768
tokens.

# The sweep

`tests/cpu.py`, five arms, three passes each over the 287-chunk corpus, each arm its own subprocess
with its own models. VRAM is read off `nvidia-smi`'s per-process table for the worker's own pid,
never off an allocator counter: the question is what the driver still refuses to hand out.

| arm | VRAM | vs baseline | wall | vs baseline | RssAnon |
|---|---|---|---|---|---|
| baseline (128) | 4,440 MiB | — | 6.19 s | — | 1,043 MB |
| 64 | 4,440 MiB | +0.0% | 6.32 s | +2.1% | 1,029 MB |
| **32** | **2,392 MiB** | **−46.1%** | 6.35 s | **+2.6%** | 897 MB |
| 16 | 2,398 MiB | −46.0% | 6.33 s | +2.3% | 881 MB |

Two facts the table settles and a design comment could not:

- **64 is not half of 128 here.** It holds the same 4,440 MiB to the megabyte. The arena is sized
  in blocks, and 64 and 128 land in the same one.
- **Below 32 there is nothing left.** 16 holds 6 MiB *more* than 32. The residue is the fp16
  weights, measured elsewhere at 2,322 MiB, plus the CUDA context. No batch size removes those.

# The threshold was unreachable, and that is recorded rather than moved

The gate written before the run was **−50% VRAM**. Every arm failed it, 32 by 3.9 points. The gate
was wrong by construction: 2,322 MiB of the 4,440 baseline is model weights, so the largest cut any
batch size can make is about −48%. A threshold set against a total that is mostly floor is a
threshold nothing can pass.

The ceiling moved to 32 anyway, on the budget that was agreed separately and *is* reachable —
throughput at or above 90% of baseline. 32 costs **+2.6%**, against +11.1% allowed.

The −50% figure is left in `tests/cpu.py` unchanged. A threshold edited after its run stops being a
threshold, and the next reader needs to see that this one failed and why.

# What this does not claim

The 4,440 MiB baseline is a **fresh subprocess doing three passes**. The live daemon was measured
at 12,850 MiB after hours. The ORT BFC arena never shrinks, so the daemon's figure is the
high-water mark of every batch shape it has ever seen, and this sweep bounds the peak of one shape
rather than that accumulation. The daemon-side number has to be re-read after this ships.

`memory.enable_memory_arena_shrinkage` under ORT 1.29.0 is untested here and is the other half of
the question. microsoft/onnxruntime#23339 reports it inert in Python against ORT 1.20.1, and that
report is stale enough to be worth one run.
