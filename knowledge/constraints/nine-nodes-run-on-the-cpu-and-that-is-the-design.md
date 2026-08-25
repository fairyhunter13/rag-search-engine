---
type: Constraint
resource: src/coderag/gpu.py, src/coderag/embed.py, tests/test_gpu_placement.py
title: Nine nodes run on the CPU EP in both exports, so the GPU rule is written about tensor math rather than about nodes
description: "`session.disable_cpu_ep_fallback` was measured and refuses both models outright over shape plumbing ORT pins to CPU on purpose. The enforcement that shipped is an op allowlist plus a 1% time bound, because either half alone is defeated."
tags: [gpu, onnxruntime, execution-provider, negative-result]
status: stable
generated: { by: claude/opus-5, at: 2026-08-20T00:00:00Z }
---

# The census

One real batch through each production model, ORT 1.29.0, `enable_profiling`, node events read off
the profile JSON. Measured 2026-08-20:

| model | CPU-assigned nodes | ops | CPU share of node time |
|---|---|---|---|
| embedder | 9 of ~960 | `Gather`x4, `Unsqueeze`x2, `Concat`, `Equal`, `Where` | 0.058579% |
| reranker | 9 | identical set | 0.183769% |

Largest CPU node: 42 µs. The op sets are the same in both exports, which is the point. This is not
a property of one model, it is ORT declining to write a GPU kernel for an op whose inputs are
shape scalars. A kernel for those would cost two device copies to move an integer.

# What was refuted

`session.disable_cpu_ep_fallback = "1"` is a real session config entry and upstream documents it as
*session creation fails if the EPs cannot fully support all nodes*. It was the obvious mechanism and
it is not shippable: set, it **refuses both exports at load** —

> This session contains graph nodes that are assigned to the default CPU EP, but fallback to CPU EP
> has been explicitly disabled by the user

— over the nine nodes above. There is no per-op exemption for it. Recorded here so nobody reaches
for it a third time.

# What enforces the rule instead

`gpu.check_placement(nodes, what)` reads an ORT profile and applies two conditions, because either
alone is defeated:

- an **op allowlist** (`SHAPE_ONLY_OPS`) — but `Gather` is on it and `Gather` is also how an
  embedding table is read, so a whole embedding lookup could hide behind an allowlisted name;
- a **1% bound on CPU share of node time** — but that says nothing about *which* op moved.

Together, a new op type trips the first and a large op hiding behind an old name trips the second. The
bound is five times the worse of the two measured shares and orders of magnitude under any real
tensor op.

`verify_session` cannot do this job and is not being asked to. ORT registers the CPU EP as an
implicit fallback, so a healthy session reports `[CUDA, CPU]` from `get_providers()` however many
nodes CUDA declined. The check is necessary and not sufficient, and its docstring now says so.
`tests/test_gpu_placement.py` is the only thing that opens a profile and looks. It is the gate on a
model swap.
